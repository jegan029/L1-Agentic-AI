# Data protection impact assessment: template

A starting point for the customer's privacy function. The customer is the controller of the data in its tickets and decides whether a DPIA is required and what it concludes. This template supplies the facts about the product that such an assessment needs, and marks with **[customer]** what only the customer can answer.

It is not legal advice and it is not a completed assessment.

References to rows (for example ML-02) are to the [controls matrix](controls-matrix.md), which names the code and the test behind each control and says what each does not cover.

## 1. The processing

**What the product does.** It reads incident tickets from the customer's IT service management system, matches each to a published procedure, runs the procedure's read-only checks against operations tools, optionally carries out one approved kind of change, and writes the result back to the ticket.

**Purposes.** Faster and more consistent first-line handling of operational incidents. **[customer]** State your own purposes and lawful basis.

**Deployment.** **[customer]** Single-tenant in your own infrastructure, or a hosted service. In the single-tenant shape the vendor does not process your data.

## 2. Data

| Data | Where it comes from | Where it is kept | Personal data it may contain |
|---|---|---|---|
| Ticket text and fields | Your ITSM | `incidents`, `runs` | Names, email addresses, telephone numbers and anything else a person typed into a ticket. The caller's name |
| Check results | Your operations tools | `run_steps` | User names, host names and addresses that appear in logs and tool output |
| Identity of staff who use the product | Your identity provider | `users`, `sessions` | Subject identifier, email address, display name |
| Audit log | The product | `audit_events` | Staff identifiers as actors. Free text is redacted on entry; evidence is held as a digest |
| SOP versions | Your authors and knowledge sources | `sop_versions` | Author and approver identifiers. Article text as written |
| Record of model requests | The product | `model_calls` | None. Digests, counts and identifiers only; no prompt or answer text |

**[customer]** Confirm whether your tickets contain special category data, data about customers of yours, or payment data, and how often.

## 3. Necessity and proportionality

- The product needs ticket text to match a procedure and to bind parameters. It does not need the caller's identity to do so.
- A model is optional. Every function works without one (decision D6).
- **[customer]** Assess whether each model task you intend to enable is necessary for your purpose. They can be enabled one at a time.

## 4. Recipients and transfers

| Recipient | When | What it receives |
|---|---|---|
| Your ITSM and operations tools | Always | What the procedure's steps send: queries and identifiers from the ticket |
| A model provider | Only if a tenant administrator switches model use on, and only for the tasks listed | Redacted ticket fields, redacted check summaries, redacted article text |
| The vendor | Single-tenant: never. Hosted: as processor | |

**[customer]** Name the model provider, its role (your processor or an independent controller), the transfer mechanism if it is outside your jurisdiction, and the retention it has agreed to. Then set the same region and retention in the deployment (`LLM_REGION`, `LLM_RETENTION_DAYS`) and in the tenant's policy. The product enforces the tenant's rule against what the deployment states. It cannot verify the statement with the provider.

See [sub-processors.md](sub-processors.md).

## 5. Risks to individuals and the measures

| Risk | Measure | Rows | What remains |
|---|---|---|---|
| An incident is wrongly marked resolved and a service a person depends on stays broken | Resolved is recorded only after a verification step passes; no model can decide an outcome | EX-01, ML-01 | A procedure whose verification is itself wrong. Every procedure is reviewed by a second person |
| Personal data in a ticket is disclosed to a model provider | Off by default; a tenant policy per task; redaction before sending; the caller field is never sent; every request recorded | ML-02, ML-03, ML-05 | Redaction is pattern matching. A person's name in running text, other than the caller's, a postal address, and data with no recognisable shape are sent. **[customer]** Decide whether that is acceptable for your tickets, or leave the model off |
| Data is processed in a region, or kept for a period, the customer has not agreed to | Tenant rules on region and retention, checked before every request | ML-03 | The rule is checked against the operator's statement about the provider |
| Data is kept longer than needed | **[customer]** Database retention is yours to set. The product has no retention control yet | AU-05 (planned) | Ticket text and results stay until you delete them. The audit log and the record of model requests cannot be pruned |
| Staff are monitored through the audit log | The log records who did what in the product. Access to it needs the auditor role | AC-03, AU-07 | **[customer]** Your own rules on the use of such records apply |
| A person's request for erasure cannot be met | Operational tables can be edited by you. The audit log cannot | | **[customer]** Assess with your privacy function whether an exemption applies to audit records |
| A model writes something untrue about a person in a note | Model text is labelled as unverified and is never used for a decision | ML-08 | An engineer may still read and repeat it |
| A decision about a person is made by automated means | The product makes no decision about a person. It handles operational incidents | | **[customer]** Confirm for your use |

## 6. Security of processing

Summarised in the [threat model](threat-model.md) and the [controls matrix](controls-matrix.md): tenant isolation in the database, single sign-on with role separation, credentials held as references to your secrets manager, a hash-chained audit log, and changes made only under policy, approval and a change record.

The product holds no security or compliance certification.

## 7. Outcome

**[customer]**

- Residual risk, and whether it is accepted.
- Whether prior consultation with a supervisory authority is needed.
- Model tasks approved for use, if any.
- Review date. Review again when the model, the provider, or the kinds of ticket change.
- Sign-off: data protection officer, system owner.
