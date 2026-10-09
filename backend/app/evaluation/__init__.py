"""Reproducible quality evaluation for the extraction pipeline.

* ``corpus``   -- the reference documents, their annotations, and a deterministic generator
* ``metrics``  -- field precision/recall/F1, OCR error rates, confidence calibration
* ``baseline`` -- an offline rule-based extractor (no API key needed) used as a reproducible baseline
* ``runner``   -- drives the *real* API + worker over the corpus and scores the outcome

Run it with ``python -m app.evaluation`` (see docs/EVALUATION.md).
"""
