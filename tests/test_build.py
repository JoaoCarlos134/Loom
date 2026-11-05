"""The full build's orchestration: resuming, asking, skipping, refreshing.

Steps are fakes -- the real ones are hours of network -- so what is tested is
the part that is the build's own: what it runs, in what order, what it asks
for and when, and what it remembers between runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from recsys.build import (
    STEPS,
    Build,
    Outcome,
    State,
    cf_is_fresh,
    has_kaggle_credentials,
    kaggle_refused_login,
    set_env_value,
)


class Fake:
    """Records the steps it is asked to run; fails the ones it is told to."""

    def __init__(self, fail=(), fail_once=(), raise_on=()):
        self.ran: list[str] = []
        self.fail = set(fail)
        self.fail_once = set(fail_once)
        self.raise_on = set(raise_on)

    def __call__(self, step):
        self.ran.append(step.name)
        if step.name in self.fail:
            return Outcome("error", "boom")
        if step.name in self.fail_once:
            self.fail_once.discard(step.name)
            return Outcome("error", "UnauthenticatedError: 401", {"needs_login": True})
        if step.name in self.raise_on:
            raise RuntimeError("the step itself broke")
        return Outcome("ok")


def make(tmp_path, fake, *, tmdb=True, ask_tmdb=None, ask_kaggle=None, kaggle=False,
         now=None, saved=None):
    state = State.load(tmp_path / "build-state.json")
    configured = {"tmdb": tmdb}
    def save(token):
        configured["tmdb"] = True
        if saved is not None:
            saved.append(token)
    return Build(
        state=state, run_step=fake, tmdb_configured=lambda: configured["tmdb"],
        ask_tmdb=ask_tmdb, save_tmdb=save, ask_kaggle=ask_kaggle,
        kaggle_configured=lambda: kaggle, print=lambda *_: None,
        now=now or (lambda: datetime.now(UTC)),
    )


def test_a_first_build_runs_every_step_in_dependency_order(tmp_path):
    fake = Fake()
    assert make(tmp_path, fake).run() == 0
    assert fake.ran == [s.name for s in STEPS]
    assert fake.ran.index("anime_mal_csv") < fake.ran.index("anime_anilist")
    assert fake.ran.index("tv_tmdb") < fake.ran.index("music_musicbrainz")
    assert fake.ran.index("derive") < fake.ran.index("embed")


def test_a_second_run_resumes_rather_than_repeating(tmp_path):
    make(tmp_path, Fake(fail={"book_openlibrary"})).run()
    again = Fake()
    make(tmp_path, again).run()
    # Only what failed, plus the cheap derived steps, which check their own freshness.
    assert again.ran == ["book_openlibrary", "derive", "embed", "cf"]


def test_redo_runs_a_finished_step_again(tmp_path):
    make(tmp_path, Fake()).run()
    again = Fake()
    make(tmp_path, again).run(redo={"anime_anilist"})
    assert again.ran[0] == "anime_anilist"


def test_without_a_tmdb_key_and_no_one_to_ask_the_tmdb_steps_are_skipped(tmp_path):
    fake = Fake()
    build = make(tmp_path, fake, tmdb=False, ask_tmdb=None)
    assert build.run() == 0
    assert not {"movie_tmdb", "tv_tmdb", "music_musicbrainz"} & set(fake.ran)
    assert build.state.steps["movie_tmdb"]["status"] == "skipped"
    assert "anime_anilist" in fake.ran  # the rest still runs


def test_the_tmdb_key_is_asked_for_once_and_saved(tmp_path):
    asked, saved = [], []
    fake = Fake()
    def ask():
        asked.append(1)
        return "  a-read-token  "
    make(tmp_path, fake, tmdb=False, ask_tmdb=ask, saved=saved).run()
    assert asked == [1] and saved == ["a-read-token"]
    assert "movie_tmdb" in fake.ran


def test_declining_the_tmdb_key_asks_only_once(tmp_path):
    asked = []
    def ask():
        asked.append(1)
        return ""
    make(tmp_path, Fake(), tmdb=False, ask_tmdb=ask).run()
    assert asked == [1]


def test_a_refused_kaggle_download_asks_for_a_login_and_retries(tmp_path):
    fake = Fake(fail_once={"anime_mal_csv"})
    asked = []
    def ask():
        asked.append(1)
        return True
    build = make(tmp_path, fake, ask_kaggle=ask)
    build.run()
    assert asked == [1]
    assert fake.ran.count("anime_mal_csv") == 2
    assert build.state.steps["anime_mal_csv"]["status"] == "ok"


def test_kaggle_is_never_asked_about_when_it_did_not_object(tmp_path):
    asked = []
    make(tmp_path, Fake(), ask_kaggle=lambda: asked.append(1) or True).run()
    assert asked == []


def test_tmdb_data_older_than_six_months_is_fetched_again(tmp_path):
    """TMDB's terms forbid storing its data for longer."""
    make(tmp_path, Fake()).run()
    later = datetime.now(UTC) + timedelta(days=181)
    again = Fake()
    make(tmp_path, again, now=lambda: later).run()
    assert {"movie_tmdb", "tv_tmdb"} <= set(again.ran)
    assert "book_goodbooks" not in again.ran


def test_status_says_what_is_done_and_what_is_left(tmp_path):
    make(tmp_path, Fake(fail={"manga_anilist"})).run()
    status = {s.name: what for s, what, _ in make(tmp_path, Fake()).status()}
    assert status["book_goodbooks"].startswith("done")
    assert status["manga_anilist"].startswith("error")


def test_a_steps_own_last_duration_becomes_its_estimate(tmp_path):
    build = make(tmp_path, Fake())
    build.run()
    step = next(s for s in STEPS if s.name == "book_goodbooks")
    assert build.state.estimate(step) < step.estimate  # the fake took ~0 s


@pytest.mark.parametrize("existing", [None, "TMDB_READ_TOKEN=\nREGION=BR\n",
                                      "TMDB_READ_TOKEN=old\nTMDB_READ_TOKEN=older\n"])
def test_the_key_is_written_into_env_once(tmp_path, existing):
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text("# Copy to .env\nTMDB_READ_TOKEN=\nREGION=BR\n", encoding="utf-8")
    if existing is not None:
        env.write_text(existing, encoding="utf-8")
    set_env_value(env, "TMDB_READ_TOKEN", "secret", example=example)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines.count("TMDB_READ_TOKEN=secret") == 1
    assert not any(line.startswith("TMDB_READ_TOKEN=") and line != "TMDB_READ_TOKEN=secret"
                   for line in lines)


def test_kaggle_credentials_are_found_where_kagglehub_looks(tmp_path):
    assert not has_kaggle_credentials({}, tmp_path)
    assert has_kaggle_credentials({"KAGGLE_USERNAME": "u", "KAGGLE_KEY": "k"}, tmp_path)
    (tmp_path / ".kaggle").mkdir()
    (tmp_path / ".kaggle" / "kaggle.json").write_text("{}", encoding="utf-8")
    assert has_kaggle_credentials({}, tmp_path)



def test_a_failure_that_is_not_a_login_refusal_never_asks_for_one(tmp_path):
    """A missing MAL_CSV_PATH file or a network error is not Kaggle asking."""
    asked = []
    make(tmp_path, Fake(fail={"anime_mal_csv"}), ask_kaggle=lambda: asked.append(1) or True).run()
    assert asked == []


@pytest.mark.parametrize(("error", "refused"), [
    ("UnauthenticatedError: You must be authenticated", True),
    ("KaggleApiHTTPError: 403 Client Error: Forbidden", True),
    ("FileNotFoundError: MAL_CSV_PATH does not exist: C:/x.csv", False),
    ("ConnectError: [Errno 11001] getaddrinfo failed", False),
    (None, False),
])
def test_only_an_authentication_error_reads_as_a_login_refusal(error, refused):
    assert kaggle_refused_login(error) is refused


def test_a_step_that_raises_is_recorded_and_the_build_goes_on(tmp_path):
    fake = Fake(raise_on={"anime_anilist"})
    build = make(tmp_path, fake)
    assert build.run() == 1
    assert build.state.steps["anime_anilist"]["status"] == "error"
    assert "manga_anilist" in fake.ran


def test_an_existing_bootstrap_counts_as_done(tmp_path):
    """The first build.py on a catalog bootstrap already built must not start
    from nothing -- and TMDB's clock starts at its real fetch."""
    long_ago = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    build = make(tmp_path, Fake())
    build.state.seed({"book_goodbooks": datetime.now(UTC).isoformat(), "movie_tmdb": long_ago})
    planned = [s.name for s in build.plan()]
    assert "book_goodbooks" not in planned
    assert "movie_tmdb" in planned  # 200 days old: past the six-month limit


def test_a_seed_never_overrides_what_build_py_recorded(tmp_path):
    make(tmp_path, Fake(fail={"book_goodbooks"})).run()
    build = make(tmp_path, Fake())
    build.state.seed({"book_goodbooks": datetime.now(UTC).isoformat()})
    assert build.state.steps["book_goodbooks"]["status"] == "error"


def test_a_failed_collaborative_build_is_never_called_fresh():
    """An error run still knew the ratings count; the old model file was still
    there. Neither makes the model current."""
    last_error = {"status": "error", "n_ratings": 10}
    assert not cf_is_fresh(last_error, model_exists=True, n_ratings=10)
    assert cf_is_fresh({"status": "ok", "n_ratings": 10}, model_exists=True, n_ratings=10)
    assert not cf_is_fresh({"status": "ok", "n_ratings": 9}, model_exists=True, n_ratings=10)
    assert not cf_is_fresh({"status": "ok", "n_ratings": 10}, model_exists=False, n_ratings=10)
