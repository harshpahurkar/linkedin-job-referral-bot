from models import Job
from scraper import _filter_and_rank, _score_job_relevance


def _job(job_id: str, title: str, company: str = "Acme", location: str = "Toronto, ON"):
    return Job(job_id, title, company, location, f"https://example.com/{job_id}")


def test_score_prefers_fresh_relevant_local_jobs():
    fresh = _job("fresh", "Software Engineer", location="Toronto, Ontario, Canada")
    stale = _job("stale", "Software Engineer", location="Remote")

    assert _score_job_relevance(fresh, ["Software Engineer"], window_idx=0) > _score_job_relevance(
        stale,
        ["Software Engineer"],
        window_idx=6,
    )


def test_filter_and_rank_removes_bad_companies_quebec_senior_and_location_dupes(monkeypatch):
    monkeypatch.setattr("scraper.Config.JOB_KEYWORDS", ["Software Engineer"])
    jobs = [
        _job("good", "Software Engineer", "GoodCo", "Toronto, ON"),
        _job("senior", "Senior Software Engineer", "GoodCo", "Toronto, ON"),
        _job("intern", "AI Software Development Intern", "GoodCo", "Toronto, ON"),
        _job("contract", "Full Stack Engineer - Freelance Contract", "GoodCo", "Canada Remote"),
        _job("quebec", "Software Engineer", "GoodCo", "Montreal, QC"),
        _job("staffing", "Software Engineer", "Robert Half", "Toronto, ON"),
        _job("aggregator", "Software Engineer", "Jobright.ai", "Canada Remote"),
        _job("dupe-remote", "Backend Developer", "DupeCo", "Remote"),
        _job("dupe-local", "Backend Developer", "DupeCo", "Toronto, ON"),
    ]
    ranked = _filter_and_rank(jobs, {job.job_id: 0 for job in jobs}, 0)
    keys = {(job.company, job.title, job.location) for job in ranked}

    assert ("GoodCo", "Software Engineer", "Toronto, ON") in keys
    assert all("Senior" not in job.title for job in ranked)
    assert all("Intern" not in job.title for job in ranked)
    assert all("Contract" not in job.title for job in ranked)
    assert all("Montreal" not in job.location for job in ranked)
    assert all(job.company != "Robert Half" for job in ranked)
    assert all(job.company != "Jobright.ai" for job in ranked)
    assert ("DupeCo", "Backend Developer", "Toronto, ON") in keys
    assert ("DupeCo", "Backend Developer", "Remote") not in keys
