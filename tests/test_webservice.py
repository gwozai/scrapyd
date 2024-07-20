import datetime
import io
import os
import re

import pytest
from twisted.web import error

from scrapyd.exceptions import DirectoryTraversalError, RunnerError
from scrapyd.interfaces import IEggStorage, IJobStorage
from scrapyd.jobstorage import Job
from scrapyd.txapp import application
from scrapyd.webservice import UtilsCache, get_spider_list
from tests import get_egg_data, root_add_version

job1 = Job("p1", "s1", end_time=datetime.datetime(2001, 2, 3, 4, 5, 6, 7))


@pytest.fixture()
def app(chdir):
    return application


def has_settings(root):
    # The configuration is not guaranteed to be accessible here, but it is for now.
    return root.scheduler.config.cp.has_section("settings")


def add_test_version(app, project, version, basename):
    app.getComponent(IEggStorage).put(io.BytesIO(get_egg_data(basename)), project, version)


def assert_content(txrequest, root, basename, args, expected):
    txrequest.args = args.copy()
    content = root.children[b"%b.json" % basename.encode()].render_GET(txrequest)

    assert content.pop("node_name")
    assert content == expected


def test_get_spider_list_log_stdout(app):
    add_test_version(app, "logstdout", "logstdout", "logstdout")
    spiders = get_spider_list("logstdout")

    # If LOG_STDOUT were respected, the output would be [].
    assert sorted(spiders) == ["spider1", "spider2"]


def test_get_spider_list(app):
    UtilsCache.invalid_cache("myproject")  # test_list_spiders fills cache, if run first

    # mybot.egg has two spiders, spider1 and spider2
    add_test_version(app, "myproject", "r1", "mybot")
    spiders = get_spider_list("myproject")
    assert sorted(spiders) == ["spider1", "spider2"]

    # mybot2.egg has three spiders, spider1, spider2 and spider3...
    # BUT you won't see it here because it's cached.
    # Effectivelly it's like if version was never added
    add_test_version(app, "myproject", "r2", "mybot2")
    spiders = get_spider_list("myproject")
    assert sorted(spiders) == ["spider1", "spider2"]

    # Let's invalidate the cache for this project...
    UtilsCache.invalid_cache("myproject")

    # Now you get the updated list
    spiders = get_spider_list("myproject")
    assert sorted(spiders) == ["spider1", "spider2", "spider3"]

    # Let's re-deploy mybot.egg and clear cache. It now sees 2 spiders
    add_test_version(app, "myproject", "r3", "mybot")
    UtilsCache.invalid_cache("myproject")
    spiders = get_spider_list("myproject")
    assert sorted(spiders) == ["spider1", "spider2"]

    # And re-deploying the one with three (mybot2.egg) with a version that
    # isn't the higher, won't change what get_spider_list() returns.
    add_test_version(app, "myproject", "r1a", "mybot2")
    UtilsCache.invalid_cache("myproject")
    spiders = get_spider_list("myproject")
    assert sorted(spiders) == ["spider1", "spider2"]


def test_get_spider_list_unicode(app):
    # mybotunicode.egg has two spiders, araña1 and araña2
    add_test_version(app, "myprojectunicode", "r1", "mybotunicode")
    spiders = get_spider_list("myprojectunicode")

    assert sorted(spiders) == ["araña1", "araña2"]


def test_get_spider_list_error(app):
    # mybot3.settings contains "raise Exception('This should break the `scrapy list` command')"
    add_test_version(app, "myproject3", "r1", "mybot3")
    with pytest.raises(RunnerError) as exc:
        get_spider_list("myproject3")

    assert re.search(f"Exception: This should break the `scrapy list` command{os.linesep}$", str(exc.value))


def test_utils_cache_repr():
    cache = UtilsCache()
    cache["key"] = "value"

    assert repr(cache) == "UtilsCache(cache_manager=JsonSqliteDict({'key': 'value'}))"


@pytest.mark.parametrize(
    ("method", "basename", "param", "args"),
    [
        ("POST", "schedule", "project", {}),
        ("POST", "schedule", "project", {b"spider": [b"scrapy-css"]}),
        ("POST", "schedule", "spider", {b"project": [b"quotesbot"]}),
        ("POST", "cancel", "project", {}),
        ("POST", "cancel", "project", {b"job": [b"aaa"]}),
        ("POST", "cancel", "job", {b"project": [b"quotesbot"]}),
        ("POST", "addversion", "project", {}),
        ("POST", "addversion", "project", {b"version": [b"0.1"]}),
        ("POST", "addversion", "version", {b"project": [b"quotesbot"]}),
        ("GET", "listversions", "project", {}),
        ("GET", "listspiders", "project", {}),
        ("GET", "status", "job", {}),
        ("POST", "delproject", "project", {}),
        ("POST", "delversion", "project", {}),
        ("POST", "delversion", "project", {b"version": [b"0.1"]}),
        ("POST", "delversion", "version", {b"project": [b"quotesbot"]}),
    ],
)
def test_required(txrequest, root_with_egg, method, basename, param, args):
    txrequest.args = args.copy() if args else {}
    with pytest.raises(error.Error) as exc:
        getattr(root_with_egg.children[b"%b.json" % basename.encode()], f"render_{method}")(txrequest)

    assert exc.value.status == b"200"
    assert exc.value.message == b"'%b' parameter is required" % param.encode()


def test_daemonstatus(txrequest, root_with_egg):
    expected = {"status": "ok", "running": 0, "pending": 0, "finished": 0}
    assert_content(txrequest, root_with_egg, "daemonstatus", {}, expected)

    root_with_egg.launcher.finished.add(job1)
    expected["finished"] += 1
    assert_content(txrequest, root_with_egg, "daemonstatus", {}, expected)

    root_with_egg.launcher.processes[0] = job1
    expected["running"] += 1
    assert_content(txrequest, root_with_egg, "daemonstatus", {}, expected)

    root_with_egg.scheduler.queues["quotesbot"].add("quotesbot")
    expected["pending"] += 1
    assert_content(txrequest, root_with_egg, "daemonstatus", {}, expected)


@pytest.mark.parametrize(
    ("extra_args", "spiders"),
    [
        ({}, ["spider1", "spider2", "spider3"]),
        ({b"_version": [b"r1"]}, ["spider1", "spider2"]),
    ],
)
def test_list_spiders(txrequest, root, extra_args, spiders):
    UtilsCache.invalid_cache("myproject")  # test_get_spider_list fills cache

    root_add_version(root, "myproject", "r1", "mybot")
    root_add_version(root, "myproject", "r2", "mybot2")

    expected = {"status": "ok", "spiders": spiders}
    assert_content(txrequest, root, "listspiders", {b"project": [b"myproject"], **extra_args}, expected)


@pytest.mark.parametrize(
    ("args", "param"),
    [
        ({b"project": [b"nonexistent"]}, "project"),
        ({b"project": [b"myproject"], b"_version": [b"nonexistent"]}, "version"),
    ],
)
def test_list_spiders_nonexistent(txrequest, root, args, param):
    root_add_version(root, "myproject", "r1", "mybot")
    root_add_version(root, "myproject", "r2", "mybot2")

    txrequest.args = args.copy()
    with pytest.raises(error.Error) as exc:
        root.children[b"listspiders.json"].render_GET(txrequest)

    assert exc.value.status == b"200"
    assert exc.value.message == b"%b 'nonexistent' not found" % param.encode()


def test_list_versions(txrequest, root_with_egg):
    expected = {"status": "ok", "versions": ["0_1"]}
    assert_content(txrequest, root_with_egg, "listversions", {b"project": [b"quotesbot"]}, expected)


def test_list_versions_nonexistent(txrequest, root):
    expected = {"status": "ok", "versions": []}
    assert_content(txrequest, root, "listversions", {b"project": [b"localproject"]}, expected)


def test_list_projects(txrequest, root_with_egg):
    expected = {"status": "ok", "projects": ["quotesbot"]}
    if has_settings(root_with_egg):
        expected["projects"].append("localproject")
    assert_content(txrequest, root_with_egg, "listprojects", {}, expected)


def test_list_projects_empty(txrequest, root):
    expected = {"status": "ok", "projects": []}
    assert_content(txrequest, root, "listprojects", {}, expected)


def test_list_jobs(txrequest, root_with_egg):
    txrequest.args = {}
    content = root_with_egg.children[b"listjobs.json"].render_GET(txrequest)

    assert set(content) == {"node_name", "status", "pending", "running", "finished"}


def test_list_jobs_finished(txrequest, root_with_egg):
    jobstorage = root_with_egg.app.getComponent(IJobStorage)
    jobstorage.add(Job("proj1", "spider-a", "id1234"))

    txrequest.args = {}
    content = root_with_egg.children[b"listjobs.json"].render_GET(txrequest)

    assert set(content["finished"][0]) == {
        "project",
        "spider",
        "id",
        "start_time",
        "end_time",
        "log_url",
        "items_url",
    }


def test_delete_version(txrequest, root):
    root_add_version(root, "myproject", "r1", "mybot")
    root_add_version(root, "myproject", "r2", "mybot2")
    root.update_projects()

    txrequest.args = {b"project": [b"myproject"]}
    content = root.children[b"listspiders.json"].render_GET(txrequest)
    assert content["spiders"] == ["spider1", "spider2", "spider3"]

    # Delete one version/
    txrequest.args = {b"project": [b"myproject"], b"version": [b"r2"]}
    content = root.children[b"delversion.json"].render_POST(txrequest)
    assert content.pop("node_name")
    assert content == {"status": "ok"}
    assert root.eggstorage.get("myproject", "r2") == (None, None)  # version is gone

    txrequest.args = {b"project": [b"myproject"]}
    content = root.children[b"listspiders.json"].render_GET(txrequest)
    assert content["spiders"] == ["spider1", "spider2"]  # "spider3" if UtilsCache.invalid_cache() weren't called

    txrequest.args = {}
    content = root.children[b"listprojects.json"].render_GET(txrequest)
    assert content["projects"] == ["myproject"]

    # Delete another version.
    txrequest.args = {b"project": [b"myproject"], b"version": [b"r1"]}
    content = root.children[b"delversion.json"].render_POST(txrequest)
    assert content.pop("node_name")
    assert content == {"status": "ok"}
    assert root.eggstorage.get("myproject") == (None, None)  # project is gone

    txrequest.args = {}
    content = root.children[b"listprojects.json"].render_GET(txrequest)
    assert content["projects"] == []  # "myproject" if root.update_projects() weren't celled


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({b"project": [b"quotesbot"], b"version": [b"nonexistent"]}, b"version 'nonexistent' not found"),
        ({b"project": [b"nonexistent"], b"version": [b"0.1"]}, b"version '0.1' not found"),
    ],
)
def test_delete_version_nonexistent(txrequest, root_with_egg, args, message):
    txrequest.args = args.copy()
    with pytest.raises(error.Error) as exc:
        root_with_egg.children[b"delversion.json"].render_POST(txrequest)

    assert exc.value.status == b"200"
    assert exc.value.message == message


def test_delete_project(txrequest, root_with_egg):
    txrequest.args = {b"project": [b"quotesbot"]}
    content = root_with_egg.children[b"listspiders.json"].render_GET(txrequest)
    assert content["spiders"] == ["toscrape-css", "toscrape-xpath"]

    txrequest.args = {}
    content = root_with_egg.children[b"listprojects.json"].render_GET(txrequest)
    assert content["projects"] == ["quotesbot"]

    # Delete the project.
    txrequest.args = {b"project": [b"quotesbot"]}
    content = root_with_egg.children[b"delproject.json"].render_POST(txrequest)
    assert content.pop("node_name")
    assert content == {"status": "ok"}
    assert root_with_egg.eggstorage.get("quotesbot") == (None, None)  # project is gone

    txrequest.args = {b"project": [b"quotesbot"]}
    with pytest.raises(error.Error) as exc:
        root_with_egg.children[b"listspiders.json"].render_GET(txrequest)
    assert exc.value.message == b"project 'quotesbot' not found"

    txrequest.args = {}
    content = root_with_egg.children[b"listprojects.json"].render_GET(txrequest)
    assert content["projects"] == []  # "quotesbot" if root.update_projects() weren't celled


def test_delete_project_nonexistent(txrequest, root):
    txrequest.args = {b"project": [b"nonexistent"]}
    with pytest.raises(error.Error) as exc:
        root.children[b"delproject.json"].render_POST(txrequest)

    assert exc.value.status == b"200"
    assert exc.value.message == b"project 'nonexistent' not found"


def test_addversion(txrequest, root):
    txrequest.args = {b"project": [b"quotesbot"], b"version": [b"0.1"], b"egg": [get_egg_data("quotesbot")]}

    eggstorage = root.app.getComponent(IEggStorage)
    version, egg = eggstorage.get("quotesbot")
    if egg:
        egg.close()

    content = root.children[b"addversion.json"].render_POST(txrequest)
    no_version, no_egg = eggstorage.get("quotesbot")
    if no_egg:
        no_egg.close()

    assert version is None
    assert content["status"] == "ok"
    assert "node_name" in content
    assert no_version == "0_1"


def test_addversion_same(txrequest, root):
    txrequest.args = {b"project": [b"quotesbot"], b"version": [b"0.1"], b"egg": [get_egg_data("quotesbot")]}

    eggstorage = root.app.getComponent(IEggStorage)
    version, egg = eggstorage.get("quotesbot")
    if egg:
        egg.close()

    content = root.children[b"addversion.json"].render_POST(txrequest)
    no_version, no_egg = eggstorage.get("quotesbot")
    if no_egg:
        no_egg.close()

    assert version is None
    assert content["status"] == "ok"
    assert "node_name" in content
    assert no_version == "0_1"


def test_schedule(txrequest, root_with_egg):
    assert root_with_egg.scheduler.queues["quotesbot"].list() == []

    txrequest.args = {b"project": [b"quotesbot"], b"spider": [b"toscrape-css"]}
    content = root_with_egg.children[b"schedule.json"].render_POST(txrequest)
    jobs = root_with_egg.scheduler.queues["quotesbot"].list()
    jobs[0].pop("_job")

    assert len(jobs) == 1
    assert jobs[0] == {"name": "toscrape-css", "settings": {}, "version": None}
    assert content["status"] == "ok"
    assert "jobid" in content


def test_schedule_nonexistent_project(txrequest, root):
    txrequest.args = {b"project": [b"nonexistent"], b"spider": [b"toscrape-css"]}
    with pytest.raises(error.Error) as exc:
        root.children[b"schedule.json"].render_POST(txrequest)

    assert exc.value.status == b"200"
    assert exc.value.message == b"project 'nonexistent' not found"


def test_schedule_nonexistent_version(txrequest, root_with_egg):
    txrequest.args = {b"project": [b"quotesbot"], b"_version": [b"nonexistent"], b"spider": [b"toscrape-css"]}
    with pytest.raises(error.Error) as exc:
        root_with_egg.children[b"schedule.json"].render_POST(txrequest)

    assert exc.value.status == b"200"
    assert exc.value.message == b"version 'nonexistent' not found"


def test_schedule_nonexistent_spider(txrequest, root_with_egg):
    txrequest.args = {b"project": [b"quotesbot"], b"spider": [b"nonexistent"]}
    with pytest.raises(error.Error) as exc:
        root_with_egg.children[b"schedule.json"].render_POST(txrequest)

    assert exc.value.status == b"200"
    assert exc.value.message == b"spider 'nonexistent' not found"


@pytest.mark.parametrize(
    ("endpoint", "attach_egg", "method"),
    [
        (b"addversion.json", True, "render_POST"),
        (b"listversions.json", False, "render_GET"),
        (b"delproject.json", False, "render_POST"),
        (b"delversion.json", False, "render_POST"),
    ],
)
def test_project_directory_traversal(txrequest, root, endpoint, attach_egg, method):
    txrequest.args = {b"project": [b"../p"], b"version": [b"0.1"]}

    if attach_egg:
        txrequest.args[b"egg"] = [get_egg_data("quotesbot")]

    with pytest.raises(DirectoryTraversalError) as exc:
        getattr(root.children[endpoint], method)(txrequest)

    assert str(exc.value) == "../p"

    eggstorage = root.app.getComponent(IEggStorage)
    assert eggstorage.get("quotesbot") == (None, None)


@pytest.mark.parametrize(
    ("endpoint", "method"),
    [
        (b"schedule.json", "render_POST"),
        (b"listspiders.json", "render_GET"),
    ],
)
def test_project_directory_traversal_runner(txrequest, root, endpoint, method):
    txrequest.args = {b"project": [b"../p"], b"spider": [b"s"]}

    with pytest.raises(DirectoryTraversalError) as exc:
        getattr(root.children[endpoint], method)(txrequest)

    assert str(exc.value) == "../p"
