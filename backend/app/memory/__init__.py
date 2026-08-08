"""Memory subsystem.

Two tiers, both reading the same durable episodic log (RunStore):
  - memory_manager — relevance-ranked recall of past SUCCESSFUL work.
  - mistake_repository — recall of past FAILURES on similar tasks.
  - retrieval — the shared lexical ranker, and the seam where a vector
    backend would replace it once the corpus justifies one.

`embedding_store.py` used to sit here as a 0-byte file imported by
nothing, alongside an EmbeddingStoreConfig in config_loader that
defaults to disabled. It was deleted rather than filled: retrieval.rank
is the interface such a store would implement, and an empty module that
looks like a feature is worse than an absent one.
"""
