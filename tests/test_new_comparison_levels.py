import pytest

import splink.comparison_level_library as cll
import splink.comparison_library as cl

from .decorator import mark_with_dialects_excluding

comparison_first_name = {
    "output_column_name": "first_name",
    "comparison_levels": [
        cll.NullLevel("first_name"),
        cll.ExactMatchLevel("first_name", term_frequency_adjustments=True),
        cll.LevenshteinLevel("first_name", 2).configure(m_probability=0.2),
        cll.ElseLevel(),
    ],
}
comparison_surname = {
    "output_column_name": "surname",
    "comparison_levels": [
        cll.NullLevel("surname"),
        cll.ExactMatchLevel("surname", term_frequency_adjustments=True),
        cll.LevenshteinLevel("surname", 2),
        cll.ElseLevel().configure(m_probability=0.2, u_probability=0.85),
    ],
}
comparison_city = {
    "output_column_name": "city",
    "comparison_levels": [
        cll.NullLevel("city"),
        cll.ExactMatchLevel("city", term_frequency_adjustments=True),
        cll.LevenshteinLevel("city", 1),
        cll.LevenshteinLevel("city", 2),
        cll.ElseLevel(),
    ],
}
comparison_email = {
    "output_column_name": "dob",
    "comparison_levels": [
        cll.NullLevel("dob"),
        cll.ExactMatchLevel("dob"),
        cll.CustomLevel("substr(dob_l, 1, 4) = substr(dob_r, 1, 4)", "year matches"),
        {
            "sql_condition": "substr(dob_l, 1, 2) = substr(dob_r, 1, 2)",
            "label_for_charts": "century matches",
        },
        cll.LevenshteinLevel("dob", 3),
        cll.ElseLevel(),
    ],
}

cll_settings = {
    "link_type": "dedupe_only",
    "comparisons": [
        comparison_first_name,
        comparison_surname,
        comparison_city,
        comparison_email,
    ],
    "blocking_rules_to_generate_predictions": [
        "l.dob = r.dob",
        "l.first_name = r.first_name",
    ],
}


@mark_with_dialects_excluding()
def test_cll_creators_run_predict(dialect, test_helpers):
    helper = test_helpers[dialect]
    df = helper.load_frame_from_csv("./tests/datasets/fake_1000_from_splink_demos.csv")

    linker = helper.Linker(df, cll_settings, **helper.extra_linker_args())
    linker.predict()


@mark_with_dialects_excluding()
def test_cll_creators_instantiate_levels(dialect):
    cll.NullLevel("city").get_comparison_level(dialect)
    cll.ElseLevel().get_comparison_level(dialect)
    cll.ExactMatchLevel("city").get_comparison_level(dialect)
    cll.LevenshteinLevel("city", 5).get_comparison_level(dialect)


comparison_name = cl.CustomComparison(
    "name",
    [
        cll.CustomLevel(
            "(first_name_l IS NULL OR first_name_r IS NULL) AND "
            "(surname_l IS NULL OR surname_r IS NULL) "
        ).configure(is_null_level=True),
        {
            "sql_condition": (
                "concat(first_name_l, surname_l) = concat(first_name_r, surname_r)"
            ),
            "label_for_charts": "both names matching",
        },
        cll.CustomLevel(
            (
                "levenshtein("
                "concat(first_name_l, surname_l), "
                "concat(first_name_r, surname_r)"
                ") <= 3"
            ),
            "both names fuzzy matching",
        ),
        cll.ExactMatchLevel("first_name"),
        cll.ExactMatchLevel("surname"),
        cll.ElseLevel(),
    ],
)
comparison_city = cl.ExactMatch("city").configure(u_probabilities=[0.6, 0.4])
comparison_email = cl.LevenshteinAtThresholds("email", 3).configure(
    m_probabilities=[0.8, 0.1, 0.1]
)

cl_settings = {
    "link_type": "dedupe_only",
    "comparisons": [
        comparison_name,
        comparison_city,
        comparison_email,
        comparison_dob,
    ],
    "blocking_rules_to_generate_predictions": [
        "l.dob = r.dob",
        "l.first_name = r.first_name",
    ],
}


@mark_with_dialects_excluding()
def test_cl_creators_run_predict(dialect, test_helpers):
    helper = test_helpers[dialect]
    df = helper.load_frame_from_csv("./tests/datasets/fake_1000_from_splink_demos.csv")

    linker = helper.Linker(df, cl_settings, **helper.extra_linker_args())

    linker.predict()


def test_cl_configure():
    # this is fine:
    cl.LevenshteinAtThresholds("col", [1, 2, 3]).configure(
        m_probabilities=[0.4, 0.1, 0.1, 0.3, 0.1]
    )

    with pytest.raises(ValueError):
        # too many probabilities
        cl.ExactMatch("col").configure(m_probabilities=[0.5, 0.3, 0.2])

    with pytest.raises(ValueError):
        # too few probabilities
        cl.LevenshteinAtThresholds("col", [1, 2]).configure(u_probabilities=[0.5, 0.5])
