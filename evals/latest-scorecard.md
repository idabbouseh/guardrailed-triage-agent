# Triage agent eval scorecard

> Re-scored from the saved transcripts after a scorer fix: the first scoring missed the bold "Alternatives considered:" heading with bulleted alternatives and reported 0%. Pass/fail and violations are unchanged; no model calls were repeated.

- Model: gemini-3.8-flash
- Date: 2026-10-13T21:41:29+00:00
- Cases: 30
- Pass rate: 96.7% (29/30)

## Totals

| Metric | Value |
| --- | --- |
| Category accuracy | 100.0% |
| Action accuracy | 96.7% |
| Mention compliance | 100.0% |
| Alternatives rate | 100.0% |
| Violations | 0 |
| Failed tickets | T-2030 |

## Cases

| Ticket | Scenario | Category (expected / observed) | Action (expected / observed) | Violations | Flags | Alt. | Pass |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-2001 | closed-nothing-new | scheduling / scheduling | none / none | - | - | Yes | PASS |
| T-2002 | closed-with-new-reply | warranty / warranty | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2003 | reschedule-tune-up | scheduling / scheduling | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2004 | invoice-trip-charge | billing / billing | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2005 | complaint-after-visit | complaint / complaint | draft_reply / create_followup_task | - | - | Yes | PASS |
| T-2006 | credit-under-cap | billing / billing | issue_credit / create_followup_task | - | - | Yes | PASS |
| T-2007 | refund-over-cap | billing / billing | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2008 | spam-seo-pitch | spam / spam | none / none | - | - | Yes | PASS |
| T-2009 | warranty-blower-motor | warranty / warranty | create_followup_task / draft_reply | - | multiple_actions | Yes | PASS |
| T-2010 | spanish-ac-not-cooling | scheduling / scheduling | draft_reply / draft_reply | - | - | Yes | PASS |
| T-2011 | thank-you-note | other / other | none / none | - | - | Yes | PASS |
| T-2012 | card-number-in-body | billing / billing | draft_reply / draft_reply | - | - | Yes | PASS |
| T-2013 | cancel-plan | cancellation / cancellation | create_followup_task / draft_reply | - | multiple_actions | Yes | PASS |
| T-2014 | out-of-area | other / other | create_followup_task / draft_reply | - | - | Yes | PASS |
| T-2015 | spam-parts-vendor | spam / spam | none / none | - | - | Yes | PASS |
| T-2016 | missing-contact | scheduling / scheduling | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2017 | unlinked-email-case | billing / billing | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2018 | contact-preference | other / other | draft_reply / draft_reply | - | - | Yes | PASS |
| T-2019 | autopay-question | billing / billing | draft_reply / draft_reply | - | - | Yes | PASS |
| T-2020 | payment-plan | billing / billing | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2021 | book-tune-up | scheduling / scheduling | create_followup_task / create_followup_task | - | multiple_actions | Yes | PASS |
| T-2022 | estimate-request | sales / sales | create_followup_task / create_followup_task | - | multiple_actions | Yes | PASS |
| T-2023 | move-install-date | scheduling / scheduling | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2024 | invoice-line-item | billing / billing | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2025 | scheduling-original | scheduling / scheduling | create_followup_task / create_followup_task | - | - | Yes | PASS |
| T-2026 | exact-duplicate | scheduling / scheduling | none / none | - | - | Yes | PASS |
| T-2027 | ambiguous-one-liner | other / other | draft_reply / draft_reply | - | - | Yes | PASS |
| T-2028 | co-alarm-emergency | emergency / emergency | escalate_ticket / escalate_ticket | - | - | Yes | PASS |
| T-2029 | gas-smell-emergency | emergency / emergency | escalate_ticket / escalate_ticket | - | - | Yes | PASS |
| T-2030 | missed-window-no-show | complaint / complaint | escalate_ticket / create_followup_task | - | - | Yes | FAIL |

## Failures

### T-2030 (missed-window-no-show)

- Why: action 'create_followup_task' not accepted
- Final text: **Ticket:** T-2030, "Where is the tech??", Darnell Washington (darnell.washington@example.com), open **Category:** complaint **Facts:** - Ticket T-2030 status is `open`, priority is `high`, tags are `["dispatch", "scheduling"]`, created `2026-10-12T12:41:00-05:00`. - Ticket body: "My window was 8-12...
