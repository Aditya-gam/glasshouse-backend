"""Unit (M1.4): the Reddit + X upload adapters parse real export formats → canonical items.

Pure — small in-memory export zips fed through the M1.1 port + run_ingestion (parse→normalize→drop).
"""

import io
import json
import zipfile
from datetime import UTC, datetime

from app.ingestion.sources.reddit_export import RedditExportAdapter
from app.ingestion.sources.x_archive import XArchiveAdapter
from app.services.ingestion import run_ingestion


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_reddit_export_parses_comments_and_posts() -> None:
    comments = (
        "id,date,subreddit,body\n"
        "c1,2023-01-15 12:34:56 UTC,Seattle,Ugh the 405 traffic again this morning.\n"
    )
    posts = (
        "id,date,subreddit,title,body\n"
        "p1,2023-02-01 09:00:00 UTC,Seattle,My commute,Walking to Gas Works Park before standup.\n"
    )
    archive = _zip({"comments.csv": comments, "posts.csv": posts})

    items = run_ingestion(RedditExportAdapter(archive))

    assert len(items) == 2
    assert all(i.platform == "reddit" and i.is_subject_authored for i in items)
    by_text = {i.text: i for i in items}
    assert "Ugh the 405 traffic again this morning." in by_text
    assert "My commute Walking to Gas Works Park before standup." in by_text  # title + body
    comment = by_text["Ugh the 405 traffic again this morning."]
    assert comment.posted_at == datetime(2023, 1, 15, 12, 34, 56, tzinfo=UTC)


def test_x_archive_parses_tweets_and_drops_retweets() -> None:
    tweets_js = "window.YTD.tweets.part0 = " + json.dumps(
        [
            {
                "tweet": {
                    "full_text": "Walking to Gas Works Park in Seattle this morning.",
                    "created_at": "Wed Jan 15 12:34:56 +0000 2023",
                }
            },
            {
                "tweet": {
                    "full_text": "RT @someone: a third-party post we must never store",
                    "created_at": "Thu Jan 16 08:00:00 +0000 2023",
                }
            },
        ]
    )
    archive = _zip({"data/tweets.js": tweets_js})

    items = run_ingestion(XArchiveAdapter(archive))

    # The retweet is third-party (RT @…) → dropped; only the user's own tweet survives.
    assert len(items) == 1
    own = items[0]
    assert own.platform == "x" and own.is_subject_authored is True
    assert "Gas Works Park" in own.text
    assert own.posted_at == datetime(2023, 1, 15, 12, 34, 56, tzinfo=UTC)  # +0000 → UTC


def test_adapters_tolerate_missing_files() -> None:
    # An archive without the expected files yields nothing rather than raising.
    assert run_ingestion(RedditExportAdapter(_zip({"readme.txt": "hi"}))) == []
    assert run_ingestion(XArchiveAdapter(_zip({"readme.txt": "hi"}))) == []
