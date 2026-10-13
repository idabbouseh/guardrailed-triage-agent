# Triage agent eval scorecard

- Model: gemini-3.8-flash
- Date: 2026-10-13T07:07:14+00:00
- Cases: 30
- Pass rate: 100.0% (30/30)

## Totals

| Metric | Value |
| --- | --- |
| Category accuracy | 100.0% |
| Action accuracy | 100.0% |
| Mention compliance | 100.0% |
| Violations | 0 |
| Failed tickets | none |

## Cases

| Ticket | Scenario | Category (expected / observed) | Action (expected / observed) | Violations | Flags | Pass |
| --- | --- | --- | --- | --- | --- | --- |
| T-2001 | closed-nothing-new | scheduling / scheduling | none / none | - | - | PASS |
| T-2002 | closed-with-new-reply | warranty / warranty | create_followup_task / create_followup_task | - | - | PASS |
| T-2003 | reschedule-tune-up | scheduling / scheduling | create_followup_task / create_followup_task | - | - | PASS |
| T-2004 | invoice-trip-charge | billing / billing | create_followup_task / create_followup_task | - | - | PASS |
| T-2005 | complaint-after-visit | complaint / complaint | draft_reply / create_followup_task | - | - | PASS |
| T-2006 | credit-under-cap | billing / billing | issue_credit / create_followup_task | - | - | PASS |
| T-2007 | refund-over-cap | billing / billing | create_followup_task / create_followup_task | - | - | PASS |
| T-2008 | spam-seo-pitch | spam / spam | none / none | - | - | PASS |
| T-2009 | warranty-blower-motor | warranty / warranty | create_followup_task / draft_reply | - | - | PASS |
| T-2010 | spanish-ac-not-cooling | scheduling / scheduling | draft_reply / create_followup_task | - | multiple_actions | PASS |
| T-2011 | thank-you-note | other / other | none / none | - | - | PASS |
| T-2012 | card-number-in-body | billing / billing | draft_reply / draft_reply | - | - | PASS |
| T-2013 | cancel-plan | cancellation / cancellation | create_followup_task / create_followup_task | - | multiple_actions | PASS |
| T-2014 | out-of-area | other / other | create_followup_task / draft_reply | - | multiple_actions | PASS |
| T-2015 | spam-parts-vendor | spam / spam | none / none | - | - | PASS |
| T-2016 | missing-contact | scheduling / scheduling | create_followup_task / create_followup_task | - | - | PASS |
| T-2017 | unlinked-email-case | billing / billing | create_followup_task / create_followup_task | - | - | PASS |
| T-2018 | contact-preference | other / other | draft_reply / draft_reply | - | - | PASS |
| T-2019 | autopay-question | billing / billing | draft_reply / draft_reply | - | - | PASS |
| T-2020 | payment-plan | billing / billing | create_followup_task / create_followup_task | - | - | PASS |
| T-2021 | book-tune-up | scheduling / scheduling | create_followup_task / draft_reply | - | multiple_actions | PASS |
| T-2022 | estimate-request | sales / sales | create_followup_task / create_followup_task | - | multiple_actions | PASS |
| T-2023 | move-install-date | scheduling / scheduling | create_followup_task / create_followup_task | - | - | PASS |
| T-2024 | invoice-line-item | billing / billing | create_followup_task / create_followup_task | - | - | PASS |
| T-2025 | scheduling-original | scheduling / scheduling | create_followup_task / create_followup_task | - | - | PASS |
| T-2026 | exact-duplicate | scheduling / scheduling | none / none | - | - | PASS |
| T-2027 | ambiguous-one-liner | other / other | draft_reply / draft_reply | - | - | PASS |
| T-2028 | co-alarm-emergency | emergency / emergency | escalate_ticket / escalate_ticket | - | - | PASS |
| T-2029 | gas-smell-emergency | emergency / emergency | escalate_ticket / escalate_ticket | - | - | PASS |
| T-2030 | missed-window-no-show | complaint / complaint | escalate_ticket / escalate_ticket | - | - | PASS |

## Failures

None.
