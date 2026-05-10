# Screenshot Review Notes

Use the screenshots captured from the deployed Streamlit app as evidence in the report and presentation.

## Recommended Captions

1. **Application UI and demo database workflow**
   - Shows the deployed Streamlit interface, Gemini model selection, API key status, Schema RAG toggle, demo database selector, and sample question workflow.
   - Use in the presentation when introducing the prototype.

2. **Natural-language query result and generated SQL**
   - Shows the Retail Analytics demo answering: "Which return reasons occur most often?"
   - Result table: Changed Mind, Other, Late Delivery, Damaged, Wrong Size.
   - Generated SQL demonstrates transparent LLM output before/after execution.
   - Use in the report methodology/results section.

3. **Retrieved schema context**
   - Shows only selected relevant schema snippets, such as `returns`, `customer_support_tickets`, and `customers`.
   - This demonstrates schema grounding and RAG-based context reduction.
   - Use when explaining the difference between static schema injection and RAG.

4. **Evaluation dashboard**
   - Shows Gold SQL baseline metrics:
     - Safe SQL: 12/12
     - Executed: 12/12
     - Value match: 12/12
     - Schema recall: 1.00
     - Exact: 12/12
   - Use in the empirical evaluation section.

## Notes

- If the RAG report still shows `hybrid-bm25-graph`, refresh/redeploy and ask a new question. The latest code reports `hybrid-bm25-embedding-semantic-values-graph`.
- The screenshots should be inserted into the final PDF and slide deck rather than referenced only as external files.
