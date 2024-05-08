from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Sequence, TypedDict, Union

import pandas as pd

from .blocking import BlockingRule, _sql_gen_where_condition, block_using_rules_sqls
from .misc import calculate_cartesian, calculate_reduction_ratio
from .pipeline import CTEPipeline
from .vertically_concatenate import compute_df_concat, enqueue_df_concat

# https://stackoverflow.com/questions/39740632/python-type-hinting-without-cyclic-imports
if TYPE_CHECKING:
    from .linker import Linker


def number_of_comparisons_generated_by_blocking_rule_post_filters_sql(
    linker: Linker,
    blocking_rule: str,
) -> str:
    settings_obj = linker._settings_obj

    where_condition = _sql_gen_where_condition(
        settings_obj._link_type,
        settings_obj.column_info_settings.unique_id_input_columns,
    )

    sql = f"""
    select count(*) as count_of_pairwise_comparisons_generated

    from __splink__df_concat as l
    inner join __splink__df_concat as r
    on
    {blocking_rule}
    {where_condition}
    """

    return sql


class CumulativeComparisonsDict(TypedDict):
    row_count: int
    rule: str
    cumulative_rows: int
    cartesian: int
    reduction_ratio: str
    start: int


def cumulative_comparisons_generated_by_blocking_rules(
    linker: Linker,
    blocking_rules: Sequence[str | BlockingRule],
    output_chart: bool = True,
    return_dataframe: bool = False,
) -> pd.DataFrame | list[CumulativeComparisonsDict]:
    # Deepcopy our original linker so we can safely adjust our settings.
    # This is particularly important to ensure we don't overwrite our
    # original blocking rules.
    linker = deepcopy(linker)

    settings_obj = linker._settings_obj
    linker._settings_obj = settings_obj

    if blocking_rules:
        brs_as_objs = settings_obj._brs_as_objs(blocking_rules)
    else:
        brs_as_objs = linker._settings_obj._blocking_rules_to_generate_predictions

    # Turn tf off.  No need to apply term frequencies to perform these calcs
    settings_obj._retain_matching_columns = False
    settings_obj._retain_intermediate_calculation_columns = False
    for cc in settings_obj.comparisons:
        for cl in cc.comparison_levels:
            # TODO: ComparisonLevel: manage access
            cl._tf_adjustment_column = None

    pipeline = CTEPipeline()
    concat = compute_df_concat(linker, pipeline)

    # Calculate the Cartesian Product
    if output_chart:
        # We only need the cartesian product if we want to output the chart view

        if settings_obj._link_type == "dedupe_only":
            group_by_statement = ""
        else:
            group_by_statement = "group by source_dataset"

        pipeline = CTEPipeline([concat])

        sql = f"""
            select count(*) as count
            from {concat.physical_name}
            {group_by_statement}
        """

        pipeline.enqueue_sql(sql, "__splink__cartesian_product")
        cartesian_count = linker.db_api.sql_pipeline_to_splink_dataframe(pipeline)
        row_count_df = cartesian_count.as_record_dict()
        cartesian_count.drop_table_from_database_and_remove_from_cache()

        cartesian = calculate_cartesian(row_count_df, settings_obj._link_type)

    # Calculate the total number of rows generated by each blocking rule

    # Note two dataset link only is not currently supported
    link_type = settings_obj._link_type

    pipeline = CTEPipeline([concat])
    sql_infos = block_using_rules_sqls(
        linker,
        input_tablename_l="__splink__df_concat",
        input_tablename_r="__splink__df_concat",
        blocking_rules=brs_as_objs,
        link_type=link_type,
    )
    pipeline.enqueue_list_of_sqls(sql_infos)

    sql = """
        select
        count(*) as row_count,
        match_key
        from __splink__df_blocked
        group by match_key
        order by cast(match_key as int) asc
    """
    pipeline.enqueue_sql(sql, "__splink__df_count_cumulative_blocks")

    cumulative_blocking_rule_count = linker.db_api.sql_pipeline_to_splink_dataframe(
        pipeline
    )
    br_n = cumulative_blocking_rule_count.as_pandas_dataframe()
    # not all dialects return column names when frame is empty (e.g. sqlite, postgres)
    if br_n.empty:
        br_n["row_count"] = []
        br_n["match_key"] = []
    cumulative_blocking_rule_count.drop_table_from_database_and_remove_from_cache()
    br_count, br_keys = list(br_n["row_count"]), list(br_n["match_key"].astype("int"))

    if len(br_count) != len(brs_as_objs):
        missing_br = [x for x in range(len(brs_as_objs)) if x not in br_keys]
        for n in missing_br:
            br_count.insert(n, 0)

    br_comparisons = []
    cumulative_sum = 0
    # Wrap everything into an output dictionary
    for row, br in zip(br_count, brs_as_objs):
        out_dict = {
            "row_count": row,
            "rule": br.blocking_rule_sql,
        }
        if output_chart:
            cumulative_sum += row
            # Increase round threshold to capture more info on larger datasets
            rr = round(calculate_reduction_ratio(cumulative_sum, cartesian), 6)

            rr_text = (
                "The rolling reduction ratio with your given blocking rule(s) "
                f"is {rr}. This represents the reduction in the total number "
                "of comparisons due to your rule(s)."
            )

            additional_vals = {
                "cumulative_rows": cumulative_sum,
                "cartesian": int(cartesian),
                "reduction_ratio": rr_text,
                "start": cumulative_sum - row,
            }
            out_dict = {**out_dict, **additional_vals}

        br_comparisons.append(out_dict.copy())

    if return_dataframe:
        return pd.DataFrame(br_comparisons)
    else:
        return br_comparisons


def count_comparisons_from_blocking_rule_pre_filter_conditions_sqls(
    linker: "Linker", blocking_rule: Union[str, "BlockingRule"]
) -> list[dict[str, str]]:
    if isinstance(blocking_rule, str):
        blocking_rule = BlockingRule(blocking_rule, sqlglot_dialect=linker._sql_dialect)

    join_conditions = blocking_rule._equi_join_conditions

    l_cols_sel = []
    r_cols_sel = []
    l_cols_gb = []
    r_cols_gb = []
    using = []
    for (
        i,
        (l_key, r_key),
    ) in enumerate(join_conditions):
        l_cols_sel.append(f"{l_key} as key_{i}")
        r_cols_sel.append(f"{r_key} as key_{i}")
        l_cols_gb.append(l_key)
        r_cols_gb.append(r_key)
        using.append(f"key_{i}")

    l_cols_sel_str = ", ".join(l_cols_sel)
    r_cols_sel_str = ", ".join(r_cols_sel)
    l_cols_gb_str = ", ".join(l_cols_gb)
    r_cols_gb_str = ", ".join(r_cols_gb)
    using_str = ", ".join(using)

    sqls = []

    if linker._two_dataset_link_only:
        #    Can just use the raw input datasets
        keys = list(linker._input_tables_dict.keys())
        input_tablename_l = linker._input_tables_dict[keys[0]].physical_name
        input_tablename_r = linker._input_tables_dict[keys[1]].physical_name

    else:
        input_tablename_l = "__splink__df_concat"
        input_tablename_r = "__splink__df_concat"

    if not join_conditions:
        if linker._two_dataset_link_only:
            sql = f"""
            SELECT
                (SELECT COUNT(*) FROM {input_tablename_l})
                *
                (SELECT COUNT(*) FROM {input_tablename_r})
                    AS count_of_pairwise_comparisons_generated
            """
        else:
            sql = """
            select count(*) * count(*) as count_of_pairwise_comparisons_generated
            from __splink__df_concat

            """
        sqls.append(
            {"sql": sql, "output_table_name": "__splink__total_of_block_counts"}
        )
        return sqls

    sql = f"""
    select {l_cols_sel_str}, count(*) as count_l
    from {input_tablename_l}
    group by {l_cols_gb_str}
    """

    sqls.append(
        {"sql": sql, "output_table_name": "__splink__count_comparisons_from_blocking_l"}
    )

    sql = f"""
    select {r_cols_sel_str}, count(*) as count_r
    from {input_tablename_r}
    group by {r_cols_gb_str}
    """

    sqls.append(
        {"sql": sql, "output_table_name": "__splink__count_comparisons_from_blocking_r"}
    )

    sql = f"""
    select *, count_l, count_r, count_l * count_r as block_count
    from __splink__count_comparisons_from_blocking_l
    inner join __splink__count_comparisons_from_blocking_r
    using ({using_str})
    """

    sqls.append({"sql": sql, "output_table_name": "__splink__block_counts"})

    sql = """
    select sum(block_count) as count_of_pairwise_comparisons_generated
    from __splink__block_counts
    """

    sqls.append({"sql": sql, "output_table_name": "__splink__total_of_block_counts"})

    return sqls


def count_comparisons_from_blocking_rule_pre_filter_conditions(
    linker: "Linker", blocking_rule: Union[str, "BlockingRule"]
) -> int:
    pipeline = CTEPipeline()
    pipeline = enqueue_df_concat(linker, pipeline)

    sqls = count_comparisons_from_blocking_rule_pre_filter_conditions_sqls(
        linker, blocking_rule
    )
    pipeline.enqueue_list_of_sqls(sqls)

    df_res = linker.db_api.sql_pipeline_to_splink_dataframe(pipeline)
    res = df_res.as_record_dict()[0]
    return int(res["count_of_pairwise_comparisons_generated"])
