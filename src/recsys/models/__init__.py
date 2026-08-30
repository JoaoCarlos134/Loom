"""Models: what turns the catalog into recommendations.

Reads the catalog, writes derived artifacts under ``DATA_DIR``, and never
modifies ``items``. Nothing here is part of an ingest -- rebuilding a model is a
separate, expensive step with its own lifecycle.
"""
