"""Celery app for a bounded beat check: the application's real tasks and schedule, with short intervals.

Production runs ``celery -A app.worker.celery_app beat`` with the real intervals. This module only changes
the interval of every entry (``BEAT_FAST_SECONDS``, default 15) so that the hour-long and day-long tasks can
be seen firing within a minute. It is for verification, not for deployment.

    cd backend && PYTHONPATH=scripts BEAT_FAST_SECONDS=15 celery -A beat_fast_app worker --loglevel=INFO &
    cd backend && PYTHONPATH=scripts BEAT_FAST_SECONDS=15 celery -A beat_fast_app beat --loglevel=INFO
"""

import os

from app.worker import celery_app

_SECONDS = float(os.environ.get("BEAT_FAST_SECONDS", "15"))

celery_app.conf.beat_schedule = {
    name: {**entry, "schedule": _SECONDS} for name, entry in celery_app.conf.beat_schedule.items()
}
