"""Image pipeline.

Read dimensions with Pillow BEFORE decoding (max-pixels guard against
decompression bombs), then CLIP zero-shot labels above a confidence
threshold; replace the file's file_labels in one transaction. Phase 7.
May depend on: repositories.labels, models_registry.
"""
