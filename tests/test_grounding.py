"""Numeric grounding check: numbers in answers must trace back to this turn's query results."""

from __future__ import annotations

import pandas as pd

from retail_agent.agent.services import Services
from retail_agent.llm.fake import call_response, text_response
from retail_agent.observability.grounding import check_grounding
from tests.test_agent_e2e import GOOD_SQL, ask, engine

Q = pd.DataFrame({"quarter": ["2026-Q1", "2025-Q4"], "revenue": [592345.58, 530545.49], "orders": [9029, 8135],
                  "return_rate": [0.1004, 0.0991]})


def test_values_rounding_suffixes_rates_and_derivations_are_grounded():
    answer = ("Q1 net revenue grew 11.6% QoQ to $592,345.58 (about $592K, or $0.59M) across 9,029 orders, "
              "up from $530,545.49. Return rate was 10.0% vs 9.9%; revenue rose by $61,800.09.")
    g = check_grounding(answer, [Q])
    assert g.checked >= 8 and g.share == 1.0, g.ungrounded


def test_shares_of_totals_and_combined_shares_are_grounded():
    status = pd.DataFrame({"status": ["Shipped", "Complete", "Processing"], "orders": [37471, 31482, 24854]})
    g = check_grounding("Shipped is 39.9% of orders; Shipped and Processing together are 66.4% (62,325 orders), "
                        "100.0% in total.", [status])
    assert g.share == 1.0, g.ungrounded


def test_invented_numbers_are_flagged():
    g = check_grounding("Revenue reached $1,234,567 with a 47.3% margin across 9,029 orders.", [Q])
    assert g.checked == 3 and g.grounded == 1 and set(g.ungrounded) == {"$1,234,567", "47.3%"}


def test_dates_years_ids_list_markers_and_small_integers_are_not_claims():
    text = "1. In 2026-10 at 21:12, customer C-ad94a816 (`trace 34aa6fda`) was in our top 10 for 2025 and Q2."
    assert check_grounding(text, [Q]).checked == 0


async def test_every_answer_with_data_gets_a_grounding_span(settings):
    services = Services.build(settings)
    eng = engine(services, call_response("run_analysis_sql", sql=GOOD_SQL, purpose="rev"),
                 text_response("Outerwear & Coats leads with revenue of $999,999,999.12."))
    r, _ = await ask(eng, "Revenue by category?")
    span = next(s for s in services.traces.spans(r.trace_id) if s["name"] == "numeric_grounding")
    assert span["status"] == "warn" and span["attributes"]["ungrounded"] == ["$999,999,999.12"]
