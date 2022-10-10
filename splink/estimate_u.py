import logging
from math import log10, ceil
from copy import deepcopy
from typing import TYPE_CHECKING

from .blocking import block_using_rules_sql, BlockingRule
from .comparison_vector_values import compute_comparison_vector_values_sql
from .expectation_maximisation import (
    compute_new_parameters_sql,
    compute_proportions_for_new_parameters,
)

from .m_u_records_to_parameters import (
    m_u_records_to_lookup_dict,
    append_u_probability_to_comparison_level_trained_probabilities,
)

# https://stackoverflow.com/questions/39740632/python-type-hinting-without-cyclic-imports
if TYPE_CHECKING:
    from .linker import Linker

logger = logging.getLogger(__name__)
logging.getLogger("splink").setLevel(logging.DEBUG)


def _num_target_rows_to_rows_to_sample(target_rows):
    # Number of rows generated by cartesian product is
    # n(n-1)/2, where n is input rows
    # We want to set a target_rows = t, the number of
    # rows generated by Splink and find out how many input rows
    # we need to generate target rows
    #     Solve t = n(n-1)/2 for n
    #     https://www.wolframalpha.com/input/?i=Solve%5Bt%3Dn+*+%28n+-+1%29+%2F+2%2C+n%5D
    sample_rows = 0.5 * ((8 * target_rows + 1) ** 0.5 + 1)
    return sample_rows


def _num_target_rows_to_pairs_to_sample(total_rows, target_rows):
    # if we are sampling k values with replacement from total_rows N
    # and dropping duplicates, the expected number of
    # unique values is N(1 - (1-1/N)^k)
    # solving this for k in terms of target number t
    # gives k = log(1 - t/N)/log(1 - 1/N)
    return ceil(log10(1 - target_rows/total_rows)/log10(1 - 1/total_rows))


def estimate_u_values(linker: "Linker", target_rows):

    logger.info("----- Estimating u probabilities using random sampling -----")

    original_settings_obj = linker._settings_obj

    training_linker = deepcopy(linker)

    sample_tables_on_link_only = True
    training_linker._train_u_using_random_sample_mode = True

    settings_obj = training_linker._settings_obj
    settings_obj._retain_matching_columns = False
    settings_obj._retain_intermediate_calculation_columns = False
    settings_obj._training_mode = True
    for cc in settings_obj.comparisons:
        for cl in cc.comparison_levels:
            cl._level_dict["tf_adjustment_column"] = None

    sql = """
    select count(*) as count
    from __splink__df_concat_with_tf
    """
    dataframe = training_linker._sql_to_splink_dataframe_checking_cache(
        sql, "__splink__df_concat_count"
    )
    result = dataframe.as_record_dict()
    dataframe.drop_table_from_database()
    count_rows = result[0]["count"]

    if settings_obj._link_type in ["dedupe_only", "link_and_dedupe"]:
        sample_size = _num_target_rows_to_rows_to_sample(target_rows)
        proportion = sample_size / count_rows

        if sample_size > count_rows:
            sample_size = count_rows

        if proportion >= 1.0:
            proportion = 1.0

        sql = f"""
        select *
        from __splink__df_concat_with_tf
        {training_linker._random_sample_sql(proportion, sample_size)}
        """
        df_sample = training_linker._sql_to_splink_dataframe_checking_cache(
            sql,
            "__splink__df_concat_with_tf_sample",
        )

    if settings_obj._link_type == "link_only":
        sql = """
        select 
        l.count as count_l, r.count as count_r
        from 
        (select count(*) as count FROM __splink_df_concat_with_tf_left) as l, 
        (select count(*) as count FROM __splink_df_concat_with_tf_right) as r
        """
        dataframe = training_linker._sql_to_splink_dataframe_checking_cache(
            sql, "__splink__df_concat_counts_lr"
        )
        result = dataframe.as_record_dict()
        dataframe.drop_table_from_database()
        count_rows = result[0]

        total_rows = count_rows["count_l"] * count_rows["count_r"]
        if target_rows >= total_rows:
            # don't need to bother with sampling in this case!
            # we will just generate all pairings
            sample_tables_on_link_only = False
            sql = """
            select * from
            __splink_df_concat_with_tf_left
            """
            df_l = training_linker._sql_to_splink_dataframe_checking_cache(
                sql, "__splink__df_concat_with_tf_left_sample"
            )
            sql = """
            select * from
            __splink_df_concat_with_tf_right
            """
            df_r = training_linker._sql_to_splink_dataframe_checking_cache(
                sql, "__splink__df_concat_with_tf_right_sample"
            )
        else:
            # strategy:
            # get sample of random row numbers from left table, and right table, of equal length,
            # allowing for repititions
            # pair them up one-to-one with a sample_id, and remove any duplicate pairings
            # then when we come to block we join oversampled tables on sample_id

            # TODO just duckdb for the mo

            # we may end up with duplicates, so we need to sample pairs more than we need
            # this calculation gives the correct _expected_ number of unique pairings
            number_of_rows_to_sample = _num_target_rows_to_pairs_to_sample(total_rows, target_rows)

            # new custom table defining the pairs of row numbers for the sample
            # __splink__df_concat_sampled_row_pairs
            sql = f"""
            select distinct row_l, row_r, first_value(sample) over(partition by row_l, row_r) as sample_id
            from (
                select 1 + floor(random() * {count_rows['count_l']})::int as row_l,
                1 + floor(random() * {count_rows['count_r']})::int as row_r,
                g.generate_series as sample
                from generate_series(1, {number_of_rows_to_sample}) g
            )
            """
            training_linker._enqueue_sql(
                sql, "__splink__df_concat_sampled_row_pairs"
            )
            df_join = training_linker._execute_sql_pipeline()

            # __splink_df_concat_with_tf_left_sample is join of _splink_df_concat_with_tf_left with above
            # and sim. for right
            sql = """
            with l as (
                select *, row_number() over () AS row_l from
                __splink_df_concat_with_tf_left
            ),
            sampled_row_pairs as (
                select row_l, sample_id from
                __splink__df_concat_sampled_row_pairs
            )
            select * from l
            right join
            sampled_row_pairs
            on l.row_l = sampled_row_pairs.row_l
            """
            df_l = training_linker._sql_to_splink_dataframe_checking_cache(
                sql, "__splink__df_concat_with_tf_left_sample"
            )

            sql = """
            with r as (
                select *, row_number() over () AS row_r from
                __splink_df_concat_with_tf_right
            ),
            sampled_row_pairs as (
                select row_r, sample_id from
                __splink__df_concat_sampled_row_pairs
            )
            select * from r
            right join
            sampled_row_pairs
            on r.row_r = sampled_row_pairs.row_r
            """
            df_r = training_linker._sql_to_splink_dataframe_checking_cache(
                sql, "__splink__df_concat_with_tf_right_sample"
            )
            df_join.drop_table_from_database()


    if settings_obj._link_type == "link_only" and sample_tables_on_link_only:
        settings_obj._blocking_rules_to_generate_predictions = [BlockingRule("l.sample_id = r.sample_id")]
    else:
        settings_obj._blocking_rules_to_generate_predictions = []

    sql = block_using_rules_sql(training_linker)
    training_linker._enqueue_sql(sql, "__splink__df_blocked")

    # repartition after blocking only exists on the SparkLinker
    repartition_after_blocking = getattr(
        training_linker, "repartition_after_blocking", False
    )

    if repartition_after_blocking:
        if settings_obj._link_type == "link_only":
            df_blocked = training_linker._execute_sql_pipeline([df_l, df_r])
        else:
            df_blocked = training_linker._execute_sql_pipeline([df_sample])
        input_dataframes = [df_blocked]
    else:
        if settings_obj._link_type == "link_only":
            input_dataframes = [df_l, df_r]
        else:
            input_dataframes = [df_sample]

    sql = compute_comparison_vector_values_sql(settings_obj)

    training_linker._enqueue_sql(sql, "__splink__df_comparison_vectors")

    sql = """
    select *, cast(0.0 as double) as match_probability
    from __splink__df_comparison_vectors
    """

    training_linker._enqueue_sql(sql, "__splink__df_predict")

    sql = compute_new_parameters_sql(settings_obj)
    linker._enqueue_sql(sql, "__splink__m_u_counts")

    df_params = training_linker._execute_sql_pipeline(input_dataframes)

    param_records = df_params.as_pandas_dataframe()
    param_records = compute_proportions_for_new_parameters(param_records)
    df_params.drop_table_from_database()

    if settings_obj._link_type == "link_only":
        df_l.drop_table_from_database()
        df_r.drop_table_from_database()
    else:
        df_sample.drop_table_from_database()

    m_u_records = [
        r
        for r in param_records
        if r["output_column_name"] != "_probability_two_random_records_match"
    ]

    m_u_records_lookup = m_u_records_to_lookup_dict(m_u_records)
    for c in original_settings_obj.comparisons:
        for cl in c._comparison_levels_excluding_null:
            append_u_probability_to_comparison_level_trained_probabilities(
                cl, m_u_records_lookup, "estimate u by random sampling"
            )

    logger.info("\nEstimated u probabilities using random sampling")
