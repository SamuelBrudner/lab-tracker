# retrieval-eval/v1

This directory is a synthetic, deterministic retrieval corpus. It contains no
production records. Each case fixes the seed, traversal, and content budgets and
declares graded seeds, typed path expectations, forbidden hard negatives,
filters, and analysis tags.

The frozen vectors exercise CI without a provider or model download. A private
real-project replay may use the same schema, but only aggregate reports from
`aggregate_only_report` may leave the local environment.

Every run supplies observations for the same five fixed strategies:
`current_decision_context`, `ranked_lexical_seeds`, `lexical_graph`,
`semantic_graph`, and `hybrid_rrf_graph`. `evaluate_strategy_matrix` rejects an
incomplete matrix so seed and traversal budgets cannot silently differ between
the reported comparisons. Semantic rollout remains blocked unless
`evaluate_semantic_rollout_gate` passes every checked-in quality threshold.
