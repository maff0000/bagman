"""CD-6 architect amendment tests — recursive Microsoft Graph folder
discovery, before the real historical sweep runs.

Covers, in order:

* `services.mailbox.microsoft.graph_client._walk_folder_tree` — the
  PURE, HTTP-free recursion algorithm (a hand-built multi-level tree:
  parent -> children -> grandchildren), proving GENUINE recursion (not
  just one level below the roots) and defensive cycle protection.
* `services.mailbox.microsoft.graph_client.classify_folders`/
  `compute_monitored_folders` — well-known-id classification (by ID,
  never by display name) and the Sent/Drafts/Outbox exclusion doctrine.
* `services.mailbox.microsoft.adapter.MicrosoftGraphMailboxAdapter
  .discover_monitored_folders` — the full, real orchestration (token
  lifecycle + list_mail_folders + resolve_well_known_folders + classify
  + monitored-set) against `FakeMicrosoftGraphClient`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.mailbox.mailbox import PROVIDER_MICROSOFT_GRAPH, InMemoryMailboxSourceRepository
from services.mailbox.microsoft.adapter import MicrosoftGraphMailboxAdapter
from services.mailbox.microsoft.fake_client import FakeMicrosoftGraphClient, FakeMicrosoftOAuthClient
from services.mailbox.microsoft.graph_client import (
    EXCLUDED_WELL_KNOWN_FOLDER_NAMES,
    WELL_KNOWN_FOLDER_NAMES_TO_RESOLVE,
    GraphFolderListResult,
    GraphFolderSummary,
    GraphOutcomeStatus,
    GraphWellKnownFoldersResult,
    MicrosoftTokenResult,
    _walk_folder_tree,
    classify_folders,
    compute_monitored_folders,
)
from services.mailbox.microsoft.secrets import InMemoryMicrosoftTokenStore

# ---------------------------------------------------------------------
# _walk_folder_tree — genuine multi-level recursion (pure, no HTTP)
# ---------------------------------------------------------------------


def test_walk_folder_tree_flattens_parent_children_and_grandchildren():
    """A THREE-level tree — proving real recursion (grandchildren are
    reached by recursing on the CHILDREN's own childFolderCount, not by
    a single fixed extra level baked into the algorithm)."""
    root = {"id": "root", "childFolderCount": 1}
    child = {"id": "child", "childFolderCount": 1}
    grandchild = {"id": "grandchild", "childFolderCount": 0}
    leaf_root = {"id": "leaf-root", "childFolderCount": 0}

    children_by_parent = {
        "root": [child],
        "child": [grandchild],
    }

    def fetch_children(folder_id):
        return children_by_parent.get(folder_id, [])

    result = _walk_folder_tree([root, leaf_root], fetch_children=fetch_children)
    ids = {f["id"] for f in result}
    assert ids == {"root", "child", "grandchild", "leaf-root"}


def test_walk_folder_tree_does_not_fetch_children_for_a_leaf():
    leaf = {"id": "leaf", "childFolderCount": 0}
    calls = []

    def fetch_children(folder_id):
        calls.append(folder_id)
        return []

    _walk_folder_tree([leaf], fetch_children=fetch_children)
    assert calls == []


def test_walk_folder_tree_guards_against_a_cycle():
    """Defensive-only (real mailboxes are finite) — a folder that
    (pathologically) reports itself as its own child must never cause
    an infinite loop."""
    a = {"id": "a", "childFolderCount": 1}

    def fetch_children(folder_id):
        return [a]  # "a" is its own child, forever

    result = _walk_folder_tree([a], fetch_children=fetch_children)
    assert [f["id"] for f in result] == ["a"]


def test_walk_folder_tree_guards_against_a_two_node_cycle():
    a = {"id": "a", "childFolderCount": 1}
    b = {"id": "b", "childFolderCount": 1}

    def fetch_children(folder_id):
        return [b] if folder_id == "a" else [a]

    result = _walk_folder_tree([a], fetch_children=fetch_children)
    assert {f["id"] for f in result} == {"a", "b"}


def test_walk_folder_tree_never_requeries_an_already_visited_folder():
    """The SAME folder reachable via two different parents (e.g.
    returned twice across paginated pages) is only ever fetched once."""
    shared = {"id": "shared", "childFolderCount": 0}
    parent_a = {"id": "parent-a", "childFolderCount": 1}
    parent_b = {"id": "parent-b", "childFolderCount": 1}
    fetch_log = []

    def fetch_children(folder_id):
        fetch_log.append(folder_id)
        if folder_id in ("parent-a", "parent-b"):
            return [shared]
        return []

    result = _walk_folder_tree([parent_a, parent_b], fetch_children=fetch_children)
    ids = [f["id"] for f in result]
    assert ids.count("shared") == 1


# ---------------------------------------------------------------------
# classify_folders / compute_monitored_folders — pure classification
# ---------------------------------------------------------------------


def _folder(folder_id, display_name, *, child_folder_count=0, is_hidden=False, parent_folder_id=None):
    return GraphFolderSummary(
        folder_id=folder_id,
        display_name=display_name,
        parent_folder_id=parent_folder_id,
        child_folder_count=child_folder_count,
        is_hidden=is_hidden,
    )


def test_classify_folders_matches_by_id_never_by_display_name():
    """A folder happening to be NAMED 'Inbox' but whose real id does
    NOT match the resolved well-known inbox id must NOT be classified
    as the well-known inbox — architect spec's explicit warning."""
    real_inbox = _folder("real-inbox-id", "Inbox")
    imposter = _folder("imposter-id", "Inbox")  # same display name, different real id
    classified = classify_folders([real_inbox, imposter], well_known_folder_ids={"inbox": "real-inbox-id"})
    by_id = {f.folder_id: f for f in classified}
    assert by_id["real-inbox-id"].well_known_name == "inbox"
    assert by_id["imposter-id"].well_known_name is None


def test_classify_folders_leaves_a_genuine_custom_folder_unclassified():
    archive = _folder("archive-id", "Archive")  # NOT one of the resolved well-known names
    classified = classify_folders([archive], well_known_folder_ids={"inbox": "some-other-id"})
    assert classified[0].well_known_name is None


def test_compute_monitored_folders_excludes_exactly_sent_drafts_outbox():
    inbox = _folder("inbox-id", "Inbox")
    junk = _folder("junk-id", "Junk Email")
    deleted = _folder("deleted-id", "Deleted Items")
    sent = _folder("sent-id", "Sent Items")
    drafts = _folder("drafts-id", "Drafts")
    outbox = _folder("outbox-id", "Outbox")
    custom_nested = _folder("custom-nested-id", "Supplier Invoices", parent_folder_id="custom-id")
    hidden = _folder("hidden-id", "Clutter", is_hidden=True)

    well_known_ids = {
        "inbox": "inbox-id", "junkemail": "junk-id", "deleteditems": "deleted-id",
        "sentitems": "sent-id", "drafts": "drafts-id", "outbox": "outbox-id",
    }
    classified = classify_folders(
        [inbox, junk, deleted, sent, drafts, outbox, custom_nested, hidden], well_known_folder_ids=well_known_ids
    )
    monitored = compute_monitored_folders(classified)
    monitored_ids = {f.folder_id for f in monitored}

    assert monitored_ids == {"inbox-id", "junk-id", "deleted-id", "custom-nested-id", "hidden-id"}
    assert EXCLUDED_WELL_KNOWN_FOLDER_NAMES == frozenset({"sentitems", "drafts", "outbox"})
    for excluded_id in ("sent-id", "drafts-id", "outbox-id"):
        assert excluded_id not in monitored_ids


def test_compute_monitored_folders_never_excludes_an_unresolved_custom_folder_that_merely_shares_a_name():
    """A real user-created 'Archive' folder is NOT one of the six
    resolved well-known names — it must be monitored, never silently
    excluded (architect spec: 'never silently exclude anything else')."""
    archive = _folder("archive-id", "Archive")
    classified = classify_folders([archive], well_known_folder_ids={})
    monitored = compute_monitored_folders(classified)
    assert [f.folder_id for f in monitored] == ["archive-id"]


def test_well_known_names_to_resolve_is_exactly_the_documented_six():
    assert WELL_KNOWN_FOLDER_NAMES_TO_RESOLVE == ("inbox", "junkemail", "deleteditems", "sentitems", "drafts", "outbox")


# ---------------------------------------------------------------------
# MicrosoftGraphMailboxAdapter.discover_monitored_folders — full wiring
# ---------------------------------------------------------------------


@pytest.fixture
def mailbox_repo():
    return InMemoryMailboxSourceRepository()


@pytest.fixture
def mailbox(mailbox_repo):
    return mailbox_repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_MICROSOFT_GRAPH
    )


@pytest.fixture
def oauth_client():
    return FakeMicrosoftOAuthClient()


@pytest.fixture
def graph_client():
    return FakeMicrosoftGraphClient()


@pytest.fixture
def token_store(mailbox):
    store = InMemoryMicrosoftTokenStore()
    store.write(
        mailbox.mailbox_id, access_token="at", refresh_token="rt",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return store


@pytest.fixture
def adapter(oauth_client, graph_client, token_store, mailbox_repo):
    return MicrosoftGraphMailboxAdapter(
        oauth_client=oauth_client, graph_client=graph_client, token_store=token_store, mailbox_repository=mailbox_repo
    )


def test_discover_monitored_folders_runs_the_full_pipeline(adapter, graph_client, mailbox):
    graph_client.queue_folder_list_result(
        GraphFolderListResult(
            status=GraphOutcomeStatus.OK,
            folders=(
                _folder("inbox-id", "Inbox"),
                _folder("junk-id", "Junk Email"),
                _folder("deleted-id", "Deleted Items"),
                _folder("sent-id", "Sent Items"),
                _folder("drafts-id", "Drafts"),
                _folder("outbox-id", "Outbox"),
                _folder("custom-id", "Supplier Invoices", child_folder_count=1),
                _folder("custom-child-id", "2026", parent_folder_id="custom-id"),
            ),
        )
    )
    graph_client.queue_well_known_folders_result(
        GraphWellKnownFoldersResult(
            status=GraphOutcomeStatus.OK,
            folder_ids={
                "inbox": "inbox-id", "junkemail": "junk-id", "deleteditems": "deleted-id",
                "sentitems": "sent-id", "drafts": "drafts-id", "outbox": "outbox-id",
            },
        )
    )
    result = adapter.discover_monitored_folders(mailbox_id=mailbox.mailbox_id)
    assert result.status == GraphOutcomeStatus.OK
    monitored_ids = {f.folder_id for f in result.folders}
    assert monitored_ids == {"inbox-id", "junk-id", "deleted-id", "custom-id", "custom-child-id"}
    # Well-known names were resolved exactly once, for exactly the
    # documented six names.
    assert graph_client.well_known_folder_calls == [WELL_KNOWN_FOLDER_NAMES_TO_RESOLVE]
    assert graph_client.folder_list_calls == 1


def test_discover_monitored_folders_never_leaks_graph_specific_fields():
    """`MonitoredFolder` is deliberately provider-neutral shaped — just
    `folder_id`/`display_name`, never `well_known_name`/`is_hidden`."""
    from services.mailbox.microsoft.adapter import MonitoredFolder

    assert set(MonitoredFolder.__dataclass_fields__.keys()) == {"folder_id", "display_name"}


def test_discover_monitored_folders_auth_error_at_list_folders_triggers_reactive_refresh(adapter, graph_client, oauth_client, token_store, mailbox):
    from services.mailbox.microsoft.fake_client import fake_token_bundle

    graph_client.queue_folder_list_result(GraphFolderListResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail="401"))
    oauth_client.queue_refresh_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle(access_token="rotated")))
    graph_client.queue_folder_list_result(GraphFolderListResult(status=GraphOutcomeStatus.OK, folders=(_folder("inbox-id", "Inbox"),)))
    graph_client.queue_well_known_folders_result(GraphWellKnownFoldersResult(status=GraphOutcomeStatus.OK, folder_ids={"inbox": "inbox-id"}))

    result = adapter.discover_monitored_folders(mailbox_id=mailbox.mailbox_id)
    assert result.status == GraphOutcomeStatus.OK
    assert graph_client.folder_list_calls == 2


def test_discover_monitored_folders_non_ok_well_known_resolution_bubbles_up(adapter, graph_client, mailbox):
    graph_client.queue_folder_list_result(GraphFolderListResult(status=GraphOutcomeStatus.OK, folders=(_folder("inbox-id", "Inbox"),)))
    graph_client.queue_well_known_folders_result(GraphWellKnownFoldersResult(status=GraphOutcomeStatus.PROVIDER_ERROR, error_detail="boom"))
    result = adapter.discover_monitored_folders(mailbox_id=mailbox.mailbox_id)
    assert result.status == GraphOutcomeStatus.PROVIDER_ERROR


def test_discover_monitored_folders_no_stored_tokens_is_a_clean_auth_error(adapter, mailbox_repo):
    other = mailbox_repo.create_mailbox(
        display_name="No Tokens", email_address="no-tokens@infosecurs.com", provider_kind=PROVIDER_MICROSOFT_GRAPH
    )
    result = adapter.discover_monitored_folders(mailbox_id=other.mailbox_id)
    assert result.status == GraphOutcomeStatus.AUTH_ERROR
