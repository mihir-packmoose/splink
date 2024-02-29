from __future__ import annotations

import logging
from copy import deepcopy
from typing import TYPE_CHECKING, List

from .blocking import BlockingRule, block_using_rules_sqls
from .charts import (
    m_u_parameters_interactive_history_chart,
    match_weights_interactive_history_chart,
    probability_two_random_records_match_iteration_chart,
)
from .comparison import Comparison
from .comparison_level import ComparisonLevel
from .comparison_vector_values import compute_comparison_vector_values_sql
from .constants import LEVEL_NOT_OBSERVED_TEXT
from .exceptions import EMTrainingException
from .expectation_maximisation import expectation_maximisation
from .misc import bayes_factor_to_prob, prob_to_bayes_factor
from .parse_sql import get_columns_used_from_sql
from .pipeline import SQLPipeline
from .settings import CoreModelSettings, Settings

logger = logging.getLogger(__name__)

# https://stackoverflow.com/questions/39740632/python-type-hinting-without-cyclic-imports
if TYPE_CHECKING:
    from .linker import Linker


class EMTrainingSession:
    """Manages training models using the Expectation Maximisation algorithm, and
    holds statistics on the evolution of parameter estimates.  Plots diagnostic charts
    """

    def __init__(
        self,
        linker: Linker,
        blocking_rule_for_training: BlockingRule,
        fix_u_probabilities: bool = False,
        fix_m_probabilities: bool = False,
        fix_probability_two_random_records_match: bool = False,
        comparisons_to_deactivate: list[Comparison] = None,
        comparison_levels_to_reverse_blocking_rule: list[ComparisonLevel] = None,
        estimate_without_term_frequencies: bool = False,
    ):
        logger.info("\n----- Starting EM training session -----\n")

        self._original_settings_obj = linker._settings_obj
        self._original_linker = linker
        # TODO: eventually just pass this + relevant settings:
        self.db_api = linker.db_api

        self._settings_obj = deepcopy(self._original_settings_obj)
        self.training_settings = self._settings_obj.training_settings
        self.unique_id_input_columns = (
            self._settings_obj.column_info_settings.unique_id_input_columns
        )
        core_model_settings = self._settings_obj.core_model_settings

        if not isinstance(blocking_rule_for_training, BlockingRule):
            blocking_rule_for_training = BlockingRule(blocking_rule_for_training)

        # TODO: only need this for blocking, for now
        # self.training_settings.blocking_rule_for_training = blocking_rule_for_training
        self._blocking_rule_for_training = blocking_rule_for_training
        self.training_settings.estimate_without_term_frequencies = (
            estimate_without_term_frequencies
        )

        if comparison_levels_to_reverse_blocking_rule:
            # TODO: atm this branch probably makes no sense. What would user pass?
            self._comparison_levels_to_reverse_blocking_rule = (
                comparison_levels_to_reverse_blocking_rule
            )
            raise ValueError("This path is broken for now.")
        else:
            self._comparison_levels_to_reverse_blocking_rule = Settings._get_comparison_levels_corresponding_to_training_blocking_rule(  # noqa
                blocking_rule_sql=blocking_rule_for_training.blocking_rule_sql,
                sqlglot_dialect_name=self.db_api.sql_dialect.sqlglot_name,
                comparisons=core_model_settings.comparisons,
            )

        self._settings_obj._probability_two_random_records_match = (
            self._blocking_adjusted_probability_two_random_records_match
        )

        self._training_fix_u_probabilities = fix_u_probabilities
        self._training_fix_m_probabilities = fix_m_probabilities
        self._training_fix_probability_two_random_records_match = (
            fix_probability_two_random_records_match
        )

        # Remove comparison columns which are either 'used up' by the blocking rules
        # or alternatively, if the user has manually provided a list to remove,
        # use this instead
        if not comparisons_to_deactivate:
            comparisons_to_deactivate = []
            br_cols = get_columns_used_from_sql(
                blocking_rule_for_training.blocking_rule_sql,
                self._settings_obj._sql_dialect,
            )
            for cc in core_model_settings.comparisons:
                cc_cols = cc._input_columns_used_by_case_statement
                cc_cols = [c.input_name for c in cc_cols]
                if set(br_cols).intersection(cc_cols):
                    comparisons_to_deactivate.append(cc)
        cc_names_to_deactivate = [
            cc.output_column_name for cc in comparisons_to_deactivate
        ]
        self._comparisons_that_cannot_be_estimated: list[
            Comparison
        ] = comparisons_to_deactivate

        filtered_ccs = [
            cc
            for cc in core_model_settings.comparisons
            if cc.output_column_name not in cc_names_to_deactivate
        ]

        core_model_settings.comparisons = filtered_ccs
        self._comparisons_that_can_be_estimated = filtered_ccs

        # this should be fixed:
        self.columns_to_select_for_comparison_vector_values = (
            Settings.columns_to_select_for_comparison_vector_values(
                unique_id_input_columns=self.unique_id_input_columns,
                comparisons=core_model_settings.comparisons,
                retain_matching_columns=False,
                additional_columns_to_retain=[],
                needs_matchkey_column=False,
            )
        )
        # TODO: not sure if we need to attach directly?
        self.core_model_settings = core_model_settings
        # initial params get inserted in training
        self._core_model_settings_history: List[CoreModelSettings] = []

    def _training_log_message(self):
        not_estimated = [
            cc.output_column_name for cc in self._comparisons_that_cannot_be_estimated
        ]
        not_estimated = "".join([f"\n    - {cc}" for cc in not_estimated])

        estimated = [
            cc.output_column_name for cc in self._comparisons_that_can_be_estimated
        ]
        estimated = "".join([f"\n    - {cc}" for cc in estimated])

        if self._training_fix_m_probabilities and self._training_fix_u_probabilities:
            raise ValueError("Can't train model if you fix both m and u probabilites")
        elif self._training_fix_u_probabilities:
            mu = "m probabilities"
        elif self._training_fix_m_probabilities:
            mu = "u probabilities"
        else:
            mu = "m and u probabilities"

        blocking_rule = self._blocking_rule_for_training.blocking_rule_sql

        logger.info(
            f"Estimating the {mu} of the model by blocking on:\n"
            f"{blocking_rule}\n\n"
            "Parameter estimates will be made for the following comparison(s):"
            f"{estimated}\n"
            "\nParameter estimates cannot be made for the following comparison(s)"
            f" since they are used in the blocking rules: {not_estimated}"
        )

    def _comparison_vectors(self):
        self._training_log_message()

        pipeline = SQLPipeline()
        nodes_with_tf = self._original_linker._initialise_df_concat_with_tf()

        sqls = block_using_rules_sqls(
            self._original_linker, [self._blocking_rule_for_training]
        )
        for sql in sqls:
            pipeline.enqueue_sql(sql["sql"], sql["output_table_name"])

        # repartition after blocking only exists on the SparkAPI
        repartition_after_blocking = getattr(
            self.db_api, "repartition_after_blocking", False
        )

        if repartition_after_blocking:
            df_blocked = self.db_api._execute_sql_pipeline(pipeline, [nodes_with_tf])
            input_dataframes = [nodes_with_tf, df_blocked]
        else:
            input_dataframes = [nodes_with_tf]

        sql = compute_comparison_vector_values_sql(
            self.columns_to_select_for_comparison_vector_values
        )
        pipeline.enqueue_sql(sql, "__splink__df_comparison_vectors")
        return self.db_api._execute_sql_pipeline(pipeline, input_dataframes)

    def _train(self, cvv=None):
        if cvv is None:
            cvv = self._comparison_vectors()

        # check that the blocking rule actually generates _some_ record pairs,
        # if not give the user a helpful message
        if not cvv.as_record_dict(limit=1):
            br_sql = f"`{self._blocking_rule_for_training.blocking_rule_sql}`"
            raise EMTrainingException(
                f"Training rule {br_sql} resulted in no record pairs.  "
                "This means that in the supplied data set "
                f"there were no pairs of records for which {br_sql} was `true`.\n"
                "Expectation maximisation requires a substantial number of record "
                "comparisons to produce accurate parameter estimates - usually "
                "at least a few hundred, but preferably at least a few thousand.\n"
                "You must revise your training blocking rule so that the set of "
                "generated comparisons is not empty.  You can use "
                "`linker.count_num_comparisons_from_blocking_rule()` to compute "
                "the number of comparisons that will be generated by a blocking rule."
            )

        # Compute the new params, populating the paramters in the copied settings object
        # At this stage, we do not overwrite any of the parameters
        # in the original (main) setting object
        core_model_settings_history = expectation_maximisation(
            db_api=self.db_api,
            training_settings=self.training_settings,
            core_model_settings=self.core_model_settings,
            unique_id_input_columns=self.unique_id_input_columns,
            fix_m_probabilities=self._training_fix_m_probabilities,
            fix_u_probabilities=self._training_fix_u_probabilities,
            fix_probability_two_random_records_match=self._training_fix_probability_two_random_records_match,
            df_comparison_vector_values=cvv,
        )
        self.core_model_settings = core_model_settings_history[-1]
        self._core_model_settings_history = core_model_settings_history

        rule = self._blocking_rule_for_training.blocking_rule_sql
        training_desc = f"EM, blocked on: {rule}"

        # Add m and u values to original settings
        for cc in self.core_model_settings.comparisons:
            orig_cc = self._original_settings_obj._get_comparison_by_output_column_name(
                cc.output_column_name
            )
            for cl in cc._comparison_levels_excluding_null:
                orig_cl = orig_cc._get_comparison_level_by_comparison_vector_value(
                    cl._comparison_vector_value
                )

                if not self._training_fix_m_probabilities:
                    not_observed = LEVEL_NOT_OBSERVED_TEXT
                    if cl._m_probability == not_observed:
                        orig_cl._add_trained_m_probability(not_observed, training_desc)
                        logger.info(
                            f"m probability not trained for {cc.output_column_name} - "
                            f"{cl.label_for_charts} (comparison vector value: "
                            f"{cl._comparison_vector_value}). This usually means the "
                            "comparison level was never observed in the training data."
                        )
                    else:
                        orig_cl._add_trained_m_probability(
                            cl.m_probability, training_desc
                        )

                if not self._training_fix_u_probabilities:
                    not_observed = LEVEL_NOT_OBSERVED_TEXT
                    if cl._u_probability == not_observed:
                        orig_cl._add_trained_u_probability(not_observed, training_desc)
                        logger.info(
                            f"u probability not trained for {cc.output_column_name} - "
                            f"{cl.label_for_charts} (comparison vector value: "
                            f"{cl._comparison_vector_value}). This usually means the "
                            "comparison level was never observed in the training data."
                        )
                    else:
                        orig_cl._add_trained_u_probability(
                            cl.u_probability, training_desc
                        )

    @property
    def _blocking_adjusted_probability_two_random_records_match(self):
        orig_prop_m = self._original_settings_obj._probability_two_random_records_match

        adj_bayes_factor = prob_to_bayes_factor(orig_prop_m)

        logger.log(15, f"Original prob two random records match: {orig_prop_m:.3f}")

        comp_level_infos = self._comparison_levels_to_reverse_blocking_rule

        for comp_level_info in comp_level_infos:
            cl = comp_level_info["level"]
            comparison = comp_level_info["comparison"]
            adj_bayes_factor = cl._bayes_factor * adj_bayes_factor

            logger.log(
                15,
                f"Increasing prob two random records match using "
                f"{comparison.output_column_name} - {cl.label_for_charts}"
                f" using bayes factor {cl._bayes_factor:,.3f}",
            )

        adjusted_prop_m = bayes_factor_to_prob(adj_bayes_factor)
        logger.log(
            15,
            f"\nProb two random records match adjusted for blocking on "
            f"{self._blocking_rule_for_training.blocking_rule_sql}: "
            f"{adjusted_prop_m:.3f}",
        )
        return adjusted_prop_m

    @property
    def _iteration_history_records(self):
        output_records = []

        for iteration, core_model_settings in enumerate(
            self._core_model_settings_history
        ):
            records = core_model_settings.parameters_as_detailed_records

            for r in records:
                r["iteration"] = iteration
                # TODO: why lambda from current settings, not history?
                r[
                    "probability_two_random_records_match"
                ] = self.core_model_settings.probability_two_random_records_match

            output_records.extend(records)
        return output_records

    @property
    def _lambda_history_records(self):
        output_records = []
        for i, s in enumerate(self._core_model_settings_history):
            lam = s.probability_two_random_records_match
            r = {
                "probability_two_random_records_match": lam,
                "probability_two_random_records_match_reciprocal": 1 / lam,
                "iteration": i,
            }

            output_records.append(r)
        return output_records

    def probability_two_random_records_match_iteration_chart(self):
        records = self._lambda_history_records
        return probability_two_random_records_match_iteration_chart(records)

    def match_weights_interactive_history_chart(self):
        records = self._iteration_history_records
        return match_weights_interactive_history_chart(
            records, blocking_rule=self._blocking_rule_for_training
        )

    def m_u_values_interactive_history_chart(self):
        records = self._iteration_history_records
        return m_u_parameters_interactive_history_chart(records)

    def __repr__(self):
        deactivated_cols = ", ".join(
            [cc.output_column_name for cc in self._comparisons_that_cannot_be_estimated]
        )
        blocking_rule = self._blocking_rule_for_training.blocking_rule_sql
        return (
            f"<EMTrainingSession, blocking on {blocking_rule}, "
            f"deactivating comparisons {deactivated_cols}>"
        )
