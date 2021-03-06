# required for running the test tournament
import copy
import functools
import math
import time
from dataclasses import dataclass

# required for typing
from pprint import pprint

import matplotlib.pyplot as plt  # for graphs

from nvm_lib.nvm_lib import NVMLib

from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from negmas import (
    AgentMechanismInterface,
    AspirationNegotiator,
    Breach,
    Contract,
    Issue,
    MechanismState,
    Negotiator,
    SAONegotiator,
)

from negmas.helpers import get_class, humanize_time
from scml.scml2020 import AWI, SCML2020Agent, SCML2020World
from scml.scml2020.agents import (
    DecentralizingAgent,
    IndDecentralizingAgent,
    MovingRangeAgent,
    BuyCheapSellExpensiveAgent,
    RandomAgent,
)
from scml.scml2020.common import TIME
from scml.scml2020.services.controllers import StepController, SyncController
from scml.scml2020.utils import anac2020_collusion, anac2020_std
from scml.scml2020.world import Failure
from tabulate import tabulate

from mynegotiationmanager import MyNegotiationManager
# from myothernegotiationmanager import NewStepNegotiationManager
from myindependentnegotiatonmanager import MyIndependentNegotiationManager
from SCMLContractsSigner import SCMLContractsSigner
from utils import *
from contractstest import solve_signer
import random

import pandas as pd
import numpy as np


def update_list(target_list: List[int], start_index: int, change: int):
    for i in range(start_index, len(target_list)):
        target_list[i] += change


class MontyHall(SCML2020Agent):
    """
    This is the only class you *need* to implement. The current skeleton has a
    basic do-nothing implementation.
    You can modify any parts of it as you need. You can act in the world by
    calling methods in the agent-world-interface instantiated as `self.awi`
    in your agent. See the documentation for more details

    """

    # =====================
    # Time-Driven Callbacks
    # =====================

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data = None
        self.plan = None
        self.negotiation_manager = None

    def init(self):
        """Called once after the agent-world interface is initialized"""
        super().init()

        # ================================
        # Static Information
        # ================================
        self.data = AgentProfile()
        self.data.id = self.id
        self.data.initial_balance = self.get_balance()

        self.data.input_product = self.awi.my_input_product
        self.data.output_product = self.awi.my_output_product

        self.data.n_lines = self.awi.n_lines  # production capacity
        self.data.n_processes = self.awi.n_processes  # n_products - 1
        self.data.n_products = self.awi.n_products
        self.data.n_steps = self.awi.n_steps

        self.data.supplier_list = self.awi.my_suppliers
        self.data.consumer_list = self.awi.my_consumers

        self.data.supplier_matrix = self.awi.all_suppliers  # first index is product
        self.data.consumer_matrix = self.awi.all_consumers  # first index is product
        self.data.catalog_price_list = self.awi.catalog_prices

        self.data.process = self.awi.profile.processes[0]  # is equal to input product
        self.data.production_cost = self.awi.profile.costs[0][self.data.process]
        self.data.last_day = self.data.n_steps - (self.data.n_processes - self.data.process)  # Last day to buy inputs

        self.data.agents = self.awi._world.agents

        # ================================
        # Planning Components
        # ================================
        self.plan = AgentPlan()
        current_inv = self.get_output_inventory()
        self.plan.getNVMPlan(self.awi.n_lines, self.awi.n_processes, self.awi.n_steps, self.data.input_product,
                             self.data.production_cost, self.awi.current_step, current_inv)

        self.plan.target_input = (
                self.data.n_lines * 2
        )  # How many input i need to have at each time step TODO: Predict

        self.plan.target_output = self.data.n_lines * 2  # Do not have more than this amount

        # self.plan.true_input = self.data.n_steps * [0]  # At step t, agent will have this many inputs for sure
        self.plan.expected_input = []  # At step t, agent expects to receive this many inputs
        for i in range((self.data.last_day + 1)):
            self.plan.expected_input.append(
                BuyPlan(self, self.plan.target_input, self.plan.target_output, self.data.n_lines))

        self.plan.available_output = 0
        # self.plan.expected_output = self.data.n_steps * [0]

        self.plan.available_money = self.data.initial_balance

        input_catalog_price = self.data.catalog_price_list[self.data.input_product]
        output_catalog_price = self.data.catalog_price_list[self.data.output_product]
        profit = output_catalog_price - (
                input_catalog_price + self.data.production_cost
        )  # Default profit

        self.plan.min_buy_price = 1
        self.plan.max_buy_price = input_catalog_price

        #        self.plan.max_buy_price = input_catalog_price + int(profit/2) - 1  # TODO: Predict
        self.plan.min_sell_price = output_catalog_price

        #        self.plan.min_sell_price = output_catalog_price - int(profit/2) + 1 # TODO: Predict
        self.plan.max_sell_price = (
                output_catalog_price * 2
        )

        # dictionaries to tally agents and respective number of successful/signed contracts
        self.plan.successful_contracts_agents = {key: 0 for key in (self.data.supplier_list + self.data.consumer_list)}
        self.plan.signed_contracts_agents = {key: 0 for key in (self.data.supplier_list + self.data.consumer_list)}
        # ================================
        # Negotiation Components
        # ================================
        self.negotiation_manager = MyNegotiationManager(data=self.data, plan=self.plan, awi=self.awi, agent=self)

        # ================================
        # Stats Components
        # ================================
        self.stat = AgentStatistics()
        self.stat.agent = self

        self.stat.print_supply_chain()

    # ================================
    # Dynamic Information
    # ================================

    def get_current_step(self):
        return self.awi.current_step

    def get_balance(self):
        return self.awi.state.balance

    def get_balance_change(self):
        return self.get_balance() - self.data.initial_balance

    def get_input_inventory(self):  # there is also inventory change available
        return self.awi.state.inventory[self.data.input_product]

    def get_output_inventory(self):
        return self.awi.state.inventory[self.data.output_product]

    def get_commands(
            self,
    ):  # commands[n_steps][n_lines] --> process_no : int, used to allocate lines
        return self.awi.state.commands

    def get_contracts_list(self):
        return self.awi.state.contracts

    def get_breach_level(self, agent_id):
        financial_reports = self.awi.reports_for(agent_id)
        return financial_reports[-1].breach_level  # Get last report of agent

    def step(self):
        """Called at every production step by the world
        Production scheduling and negotiations"""
        super().step()

        print("----------------------------------------------------------------AGENT ID: " + self.data.id)

        current_inv = self.get_output_inventory()
        self.plan.getNVMPlan(n_lines=self.awi.n_lines, n_processes=self.awi.n_processes, n_steps=self.awi.n_steps,
                             process=self.data.input_product,
                             production_cost=self.data.production_cost, current_time=self.awi.current_step,
                             current_inventory=current_inv)

        # #hacky fix to sell all inventory
        # self.plan.sell_plan = []
        # #self.plan.sell_plan.append(self.plan.available_output) #sell all inventory
        # self.plan.sell_plan.append(self.get_output_inventory())  # sell all inventory

        # hacky fix to just produce all raw input
        self.plan.produce_plan = []
        self.plan.produce_plan.append(self.get_input_inventory())

        # print('---------HACKY SELL PLAN:' + str(self.plan.sell_plan[0]))
        # print('---------HACKY PRODUCE PLAN:' + str(self.plan.produce_plan[0]))
        # print("---------INPUT INVENTORY: " + str(self.get_input_inventory()))
        # #print("---------AVAILABLE OUTPUT: " + str(self.plan.available_output))
        # print("---------OUTPUT INVENTORY: " + str(self.get_output_inventory()))

        self.propagate_inputs()  # Plan how much to buy at each step
        self.negotiation_manager.step()
        self.schedule_production()

        # print("Current step:", self.get_current_step())

    def propagate_inputs(self):  # each step
        excess_prev = max(self.get_input_inventory() - self.data.n_lines,
                          0)  # Assumes we can always afford production cost
        step = self.get_current_step() + 1
        while step <= self.data.last_day:
            self.plan.expected_input[step].excess_inputs = excess_prev
            excess_prev = self.plan.expected_input[step].get_excess()
            step += 1

    # ================================
    # Negotiation Control and Feedback
    # ================================

    def respond_to_negotiation_request(
            self,
            initiator: str,
            issues: List[Issue],
            annotation: Dict[str, Any],
            mechanism: AgentMechanismInterface,
    ) -> Optional[Negotiator]:
        """Called whenever an agent requests a negotiation with you.
        Return either a negotiator to accept or None (default) to reject it"""
        return self.negotiation_manager.respond_to_negotiation_request(
            initiator, issues, annotation, mechanism
        )

    def on_negotiation_failure(
            self,
            partners: List[str],
            annotation: Dict[str, Any],
            mechanism: AgentMechanismInterface,
            state: MechanismState,
    ) -> None:
        """Called when a negotiation the agent is a party of ends without
        agreement"""
        #        print("NEGOTIATION FAILED", self.get_current_step(),"Contract negotiation failed", annotation)
        self.stat.on_negotiation_failure(partners, annotation, mechanism, state)

    def on_negotiation_success(
            self, contract: Contract, mechanism: AgentMechanismInterface
    ) -> None:
        """Called when a negotiation the agent is a party of ends with
        agreement"""
        #        print("NEGOTIATION SUCCEEDED:", self.get_current_step(), "Contract negotiation succeeded", contract)

        # increment successful contract agent ID's. assumes partner list is length 2

        self.stat.on_negotiation_success(contract, mechanism)

    def on_contract_executed(self, contract: Contract) -> None:
        """Called when a contract executes successfully and fully"""
        # print("CONTRACT EXECUTED: BUY:", contract.annotation["is_buy"], contract)
        # print(f"catalog price * 3: {self.awi.catalog_prices[self.awi.my_input_product] * 3.0}")
        # if contract.agreement['unit_price'] > 100:
        # print("RIP")
        quantity = contract.agreement["quantity"]
        unit_price = contract.agreement["unit_price"]
        time = contract.agreement["time"]
        if not contract.annotation["is_buy"]:  # is sell
            self.plan.available_money += quantity * unit_price
        #            self.plan.available_output -= quantity #we do this in the contracts finalized so we don't oversell

        # this is wrong below since we only update available output in schedule_production after inputs are produced
        else:  # buy
            #            self.plan.available_money -= quantity * unit_price
            self.plan.available_output += quantity

    def on_contract_breached(
            self, contract: Contract, breaches: List[Breach], resolution: Optional[Contract]
    ) -> None:
        """Called when a breach occur. In 2020, there will be no resolution
        (i.e. resoluion is None)"""
        # print("CONTRACT BREACH: ", contract, self.data.id)

        #        if breaches[0].perpetrator == self.data.id:
        #            assert False, "You breached contract?!?!?"

        breach_level = breaches[0].level
        quantity = contract.agreement["quantity"]
        unit_price = contract.agreement["unit_price"]
        time = contract.agreement["time"]

        if contract.annotation[
            "is_buy"
        ]:  # perpetrator did not have enough inputs, lost input, gained money
            lost_count = int(
                round(breach_level * quantity)
            )  # how many inputs were failed to buy

            money_saved = lost_count * unit_price
            self.plan.available_money += money_saved
        else:  # perpetrator did not have enough outputs, lost money, gained ouputs
            output_saved = int(round(breach_level * quantity))
            lost_money = output_saved * unit_price

            self.plan.available_money += (
                    quantity * unit_price - lost_money
            )  # Update whatever we get from contract
            self.plan.available_output += output_saved

    # =============================
    # Contract Control and Feedback
    # =============================



    def calculate_agent_trust(self):
        delta = 0.2
        trust_probabilities = {}
        for agent in self.plan.successful_contracts_agents:
            agree_count = self.plan.successful_contracts_agents[agent]
            sign_count = self.plan.signed_contracts_agents[agent]
            p = 2.0
            if agree_count > 0 and (agent != "BUYER") and (agent != "SELLER"):
                p = math.sqrt((-1.0 / (2.0 * agree_count)) * math.log(delta / 2.0)) + sign_count/agree_count
            trust_probabilities[agent] = p
        return trust_probabilities

    def sign_all_contracts(
            self, contracts: List[Contract]
    ) -> List[Optional[str]]:

        max_buy_price = self.awi.catalog_prices[self.awi.my_input_product] * 3.0
        min_sell_price = self.awi.catalog_prices[self.awi.my_output_product] / 3.0

        signatures_final = [None] * len(contracts)

        map_original_indices = list(range(len(contracts)))

        # THIS FOR LOOP SENDS ERRORS BTW

        # for i in range(len(contracts)):
        #     # print(contracts[i], output[i])
        #     price = contracts[i].agreement['unit_price']
        #     # print(f"unit price: {price}")
        #     # print(f"max buy price: {max_buy_price}")
        #     if contracts[i].annotation['buyer'] == self.id:
        #         if price > max_buy_price:
        #             # print("MAX PRICE EXCEEDED")
        #             del contracts[i]
        #             del map_original_indices[i]
        #     else:
        #         if price < min_sell_price:
        #             # print("MAX PRICE EXCEEDED")
        #             del contracts[i]
        #             del map_original_indices[i]

        trust_probabilities = self.calculate_agent_trust()
        inventory = self.get_output_inventory()
        signed = SCMLContractsSigner.sign(self.data.id, contracts, trust_probabilities, inventory)
        signed_agreements = signed['list_of_signatures']

        for x in range(len(contracts)):
            if signed_agreements[x] is not None:
                contract = contracts[x]

                assert len(contract.partners) == 2
                assert contract.partners[0] != contract.partners[1]
                assert contract.partners[0] == self.data.id or contract.partners[1] == self.data.id
                if contract.partners[0] == self.data.id:
                    self.plan.successful_contracts_agents[contract.partners[1]] += 1
                else:
                    self.plan.successful_contracts_agents[contract.partners[0]] += 1

        #return signed_agreements

        for i in range(len(map_original_indices)):
            original_index = map_original_indices[i]
            signatures_final[original_index] = signed_agreements[i]

        return signatures_final
#
# <<<<<<< HEAD
#         # if no sell contracts, we do not call the signer solver
#         if len(output_offers) != 0:
#             x = solve_signer(buy_contracts=input_offers, sel_contracts=output_offers, inventory = self.get_output_inventory(), prints=False)
#             # print(f"solver signer: {x}")
#             buy_sign_plan = x[0]
#             sell_sign_plan = x[1]
#             print("buy and sell sign plans")
#             print(buy_sign_plan)
#             print(sell_sign_plan)
#
#             counter_buy: int = 0
#             counter_sell: int = 0
#             for i in range(len(contracts)):
#                 if contracts[i].annotation["is_buy"]:
#                     if buy_sign_plan[counter_buy] == 0:
#                         output[i] = None
#                     counter_buy = counter_buy + 1
#                 else:
#                     if sell_sign_plan[counter_sell] == 0:
#                         output[i] = None
#                     counter_sell = counter_sell + 1
#
#         # SELF EXPLOIT CHECK
#         # print("signatures...before")
#         # for i in range(len(contracts)):
#         #     # print(contracts[i], output[i])
#         #     price = contracts[i].agreement['unit_price']
#         #     # print(f"unit price: {price}")
#         #     # print(f"max buy price: {max_buy_price}")
#         #     if contracts[i].annotation['buyer'] == self.id:
#         #         if price > max_buy_price:
#         #             # print("MAX PRICE EXCEEDED")
#         #             output[i] = None
#         #     else:
#         #         if price < min_sell_price:
#         #             # print("MAX PRICE EXCEEDED")
#         #             output[i] = None
#
#         for i in range(len(contracts)):
#             if contracts[i].annotation['is_buy'] and (contracts[i].agreement['unit_price']) > max_buy_price:
#                 print(f" ***buy unit price: {contracts[i].agreement['unit_price']}, signed: {output[i]}")
#             if not contracts[i].annotation['is_buy'] and (contracts[i].agreement['unit_price']) < min_sell_price:
#                 print(f" ***sell unit price: {contracts[i].agreement['unit_price']}, signed: {output[i]}")
#
#         # print("signatures...final")
#         # print(contracts)
#         # print(output)
#         for i in range(len(contracts)):
#             # print(contracts[i], output[i])
#             continue
#         return output
# =======
# >>>>>>> 1c105d1c1861d353c6160f981b0dafa964e4db33

    def on_contracts_finalized(
            self,
            signed: List[Contract],
            cancelled: List[Contract],
            rejectors: List[List[str]],
    ) -> None:
        """Called to inform you about the final status of all contracts in
        a step (day)"""
        self.stat.on_contracts_finalized(signed, cancelled, rejectors)

        for contract in signed:
            # print("signed by both parties: " + str(contract) + " timestep: " + str(self.awi.current_step))
            # increment signed agents. assumes partner list is size 2.
            assert len(contract.partners) == 2
            assert contract.partners[0] != contract.partners[1]
            assert contract.partners[0] == self.data.id or contract.partners[1] == self.data.id
            if contract.partners[0] == self.data.id:
                self.plan.signed_contracts_agents[contract.partners[1]] += 1
            else:
                self.plan.signed_contracts_agents[contract.partners[0]] += 1

    

            quantity = contract.agreement["quantity"]
            time = contract.agreement["time"]
            unit_price = contract.agreement["unit_price"]
            if contract.annotation["is_buy"]:
                if time < len(self.plan.expected_input):
                    self.plan.expected_input[time].contract_inputs += quantity
                self.plan.available_money -= quantity * unit_price

            else:  # sell
                self.plan.available_output -= quantity

    # shouldn't this be order production, not schedule production?
    def schedule_production(self):
        commands = self.get_commands()[self.get_current_step()]
        input_count = self.plan.produce_plan[0]  # produce based off of the NVM plan

        balance = self.plan.available_money
        pay_count = int(
            balance / self.data.production_cost
        )  # How many can you produce with infinite production capacity
        scheduled_count = min(input_count, pay_count, self.data.n_lines)

        self.plan.available_output += scheduled_count
        self.plan.available_money -= scheduled_count * self.data.production_cost

        for i in range(scheduled_count):
            commands[i] = self.data.process

    def confirm_production(
            self, commands: np.ndarray, balance: int, inventory: np.ndarray
    ) -> np.ndarray:
        """
        Called just before productcion starts at every step allowing the
        agent to change what is to be produced in its factory on that step.

        Produce as much as you can while checking input count and available money
        """
        pass  # Not used anymore

    def on_failures(self, failures: List[Failure]) -> None:
        """Called when production fails. If you are careful in
        what you order in `confirm_production`, you should never see that."""

        assert False, "PRODUCTION FAILED?!?!?!?"

    # ==========================
    # Callback about Bankruptcy
    # ==========================

    def on_agent_bankrupt(
            self,
            agent: str,
            contracts: List[Contract],
            quantities: int,
            compensation_money: int,
    ) -> None:
        """Called whenever any agent goes bankrupt. It informs you about changes
        in future contracts you have with you (if any)."""
        print(
            "BANKRUPT:",
            agent,
            "went bankrupt :( quantity:",
            quantities,
            "compensation money:",
            compensation_money,
        )


competitors = [
    MontyHall,
    DecentralizingAgent,
    IndDecentralizingAgent,
    #        MovingRangeAgent,
    #        BuyCheapSellExpensiveAgent,
    #        RandomAgent,
]


def run(n_steps=52, n_processes=3):
    """
    **Not needed for submission.** You can use this function to test your agent.

    Args:
        n_steps:     The number of simulation steps.
        n_processes: # processes.

    Returns:
        None

    Remarks:

        - This function will take several minutes to run.
        - To speed it up, use a smaller `n_step` value

    """

    start = time.perf_counter()
    world = SCML2020World(
        **SCML2020World.generate(agent_types=competitors, n_steps=n_steps, n_processes=n_processes)
    )
    world.run()
    scores = world.scores()
    pprint(scores)
    f = open("scores", "a")
    f.write(scores)
    f.close()
    print(f"Finished in {humanize_time(time.perf_counter() - start)}")


def run_benchmark(n_games: int, n_step_range: Tuple[int, int], n_processes_range: Tuple[int, int]) -> pd.DataFrame:
    """Benchmarking function to run a bunch of games:
    Parameters:
        n_games: number of games to be run
        n_step_range: Tuple[int, int] which describes the lower bound and upper bound (inclusive) of steps desired for the games
        n_processes_range: Tuple[int, int] which describes the lower and upper bound (inclusive) of processes desired for the games
    Returns:
        df: DataFrame containing all of the scores and cumulative averages for each agent at each level"""

    def run_with_save(n_steps, n_processes) -> Dict[str, float]:
        """
        Same as run() but with save functionality for benchmarking
        Returns:
            new_dict: dictionary with all the average scores of each agent at each level
             (accumulating all xxDec@0 into just Dec@0)
        """

        start = time.perf_counter()
        world = SCML2020World(
            **SCML2020World.generate(agent_types=competitors, n_steps=n_steps, n_processes=n_processes)
        )
        world.run()

        # iterate through dictionary of agents and scores and only take "agent@x", adding up all the scores of agent@x
        scores = world.scores()  # a dict
        new_dict = {}
        agent_count_dict = {}
        for agent_name in scores:
            if agent_name[2:] in new_dict:
                agent_count_dict[agent_name[2:]] += 1
                new_dict[agent_name[2:]] += scores[agent_name]
            else:
                agent_count_dict[agent_name[2:]] = 1
                new_dict[agent_name[2:]] = scores[agent_name]

        # divide by number of occurrences of each agent at each level
        for agent_name in new_dict:
            new_dict[agent_name] = new_dict[agent_name] / agent_count_dict[agent_name]

        pprint(scores)
        pprint(f"Aggregated scores: {new_dict}")
        print(f"Finished in {humanize_time(time.perf_counter() - start)}")

        return new_dict

    t0 = time.time()
    # print("======================================BENCHMARKING STARTING======================================")

    average_score_dict = {"game length": [], "processes": []}
    columns = ["game length", "processes"]
    agent_names = []
    # make column labels for all possible competitors up to upper bound of processes range
    for i in range(n_processes_range[1]):
        for competitor in competitors:
            agent_name = competitor.__name__[0:3] + f"@{i}"
            agent_names.append(agent_name)
            columns.append(agent_name)
            average_score_dict[agent_name] = []



    # Run games and collect the data into average_score_dicts
    for i in range(n_games):
        n_steps = random.randint(n_step_range[0], n_step_range[1])
        n_processes = random.randint(n_processes_range[0], n_processes_range[1])
        print(f"game {i} starting")
        print(f"n_steps: {n_steps}")
        print(f"n_processes: {n_processes}")
        game_score_dict = run_with_save(n_steps=n_steps, n_processes=n_processes)

        average_score_dict["game length"].append(n_steps)
        average_score_dict["processes"].append(n_processes)
        for key in (average_score_dict.keys() - {"game length", "processes"}):
            if key in game_score_dict:  # add agent score to the average_score_dict if agent was in the game
                assert key in average_score_dict, "assert error: agent names are put into average_score_dict wrong"
                average_score_dict[key].append(game_score_dict[key])
            else:  # else give agent a score of NaN for this game
                average_score_dict[key].append(np.nan)

        # print(f"game {i} done")

    # Make DataFrame
    df = pd.DataFrame(average_score_dict)
    # Now we need to compute the cumulative moving average (CMA)
    for agent_name in agent_names:
        # this makes a new column with the CMA for agent_name
        df[f"CMA_{agent_name}"] = df[agent_name].expanding().mean()

    # output to csv
    df.to_csv(f"C:/Users/ED2016/Documents/SCML/scml2020/benchmarks/"
              f"games_{n_games}_step_range_{n_step_range[0]}_{n_step_range[1]}_processes_range_{n_processes_range[0]}"
              f"_{n_processes_range[1]}_trust_signer_inventory.csv")

    # print time taken
    print("======================================BENCHMARKING DONE======================================")
    time_taken = time.strftime("%H:%M:%S", time.gmtime(time.time() - t0)) # readable formatting
    print(f"Time taken to do the benchmarking: {time_taken}")

    # print the time of day
    print(time.ctime())

    return df


def run_tournament(
        competition="std",
        reveal_names=True,
        n_steps=100,
        n_configs=1,
        max_n_worlds_per_config=None,
        n_runs_per_world=1,
):
    """
    **Not needed for submission.** You can use this function to test your agent.

    Args:
        competition: The competition type to run (possibilities are std,
                     collusion).
        n_steps:     The number of simulation steps.
        n_configs:   Number of different world configurations to try.
                     Different world configurations will correspond to
                     different number of factories, profiles
                     , production graphs etc
        n_runs_per_world: How many times will each world simulation be run.

    Returns:
        None

    Remarks:

        - This function will take several minutes to run.
        - To speed it up, use a smaller `n_step` value

    """

    start = time.perf_counter()
    if competition == "std":
        results = anac2020_std(
            competitors=competitors,
            verbose=True,
            n_steps=n_steps,
            n_configs=n_configs,
            n_runs_per_world=n_runs_per_world,
            n_processes=3

        )
    elif competition == "collusion":
        results = anac2020_collusion(
            competitors=competitors,
            verbose=True,
            n_steps=n_steps,
            n_configs=n_configs,
            n_runs_per_world=n_runs_per_world,
            n_processes=3

        )
    else:
        raise ValueError(f"Unknown competition type {competition}")
    # print(tabulate(results.total_scores, headers="keys", tablefmt="psql"))
    # print(f"Finished in {humanize_time(time.perf_counter() - start)}")


def run_single_session():
    world = SCML2020World(
        **SCML2020World.generate(
            agent_types=competitors,
            n_steps=50,
            n_processes=3
        ),
        construct_graphs=True,
    )

    _, _ = world.draw()

    world.run_with_progress()

    contracts = world.contracts_df
    signed = contracts.loc[contracts.signed_at >= 0, :]
    fields = ["seller_name", "buyer_name", "delivery_time", "quantity", "unit_price",
              "signed_at", "executed", "breached", "nullified", "erred"]
    signed[fields].sort_values(["quantity", "unit_price"], ascending=False).head(10)
    df1 = signed.loc[signed.executed, fields].sort_values(["quantity", "unit_price"], ascending=False).head(10)
    df2 = signed.loc[signed.breached, fields[:-4] + ["breaches"]].sort_values(["quantity", "unit_price"],
                                                                              ascending=False).head(10)

    fig, (profit, score) = plt.subplots(1, 2, figsize=(15, 15))
    snames = sorted(world.non_system_agent_names)
    for name in snames:
        profit.plot(100.0 * (np.asarray(world.stats[f'balance_{name}']) / world.stats[f'balance_{name}'][0] - 1.0),
                    label=name)
        score.plot(100 * np.asarray(world.stats[f'score_{name}']), label=name)
    profit.set(xlabel='Simulation Step', ylabel='Player Profit Ignoring Inventory (%)')
    profit.legend(loc='lower left')
    score.set(xlabel='Simulation Step', ylabel='Player Score (%)')
    fig.show()

    fig, (score, profit) = plt.subplots(1, 2, figsize=(15, 15))
    final_scores = [world.stats[f"score_{_}"][-1] * (world.stats[f"balance_{_}"][0])
                    for _ in world.non_system_agent_names]
    final_profits = [world.stats[f"balance_{_}"][-1] - world.stats[f"balance_{_}"][0]
                     for _ in world.non_system_agent_names]
    plt.setp(score.xaxis.get_majorticklabels(), rotation=45)
    plt.setp(profit.xaxis.get_majorticklabels(), rotation=45)
    score.bar(world.non_system_agent_names, final_scores)
    profit.bar(world.non_system_agent_names, final_profits)
    score.set(ylabel="Final Unnormalized Score ($)")
    profit.set(ylabel="Final Balance  ($)")

    fig.show()


def main():
    # run()
    # run_single_session()
    # run_tournament()

    # run_with_save(n_steps=52, n_processes=3)
    run_benchmark(n_games=1, n_step_range=(50, 50), n_processes_range=(5, 5))
    # run_benchmark(n_games=100, n_step_range=(50, 70), n_processes_range=(3, 5))
    print("Finished...")


if __name__ == "__main__":
    main()
