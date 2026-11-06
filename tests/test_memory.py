import asyncio
import json

import pytest

from hivemind.memory import Memory, CONTEXT_CHARS, size
from hivemind.store import Store, uid


class SummaryProvider:
    def __init__(self):
        self.calls = []

    async def reply(self, messages, system):
        rows = json.loads(messages[0]['content'])
        self.calls.append(rows)
        return json.dumps({'title': 'Conversation archive',
                           'summary': 'Earlier discussions and decisions. ' + ' '.join(row['content'][:60] for row in rows),
                           'candidates': []})


@pytest.fixture
def memory(tmp_path):
    return Memory(Store(tmp_path / 'memory.db'))


def history(memory, count=60):
    rows = []
    for index in range(count):
        content = f'Conversation turn {index}: ordinary project discussion.'
        if index == 2:
            content = 'The Zephyr project prototype ships on November 19, after the robotics demonstration.'
        rows.append(memory.store.message('user' if index % 2 == 0 else 'assistant', content))
    return rows


def test_recent_context_keeps_full_current_prompt_under_hard_budget(memory):
    for _ in range(18):
        memory.store.message('assistant', 'prior conversation ' * 900)
    for index in range(6):
        memory.store.execute('INSERT INTO memories VALUES (?,?,?)', (uid(), 'project preference ' * 250, index))
    query = 'x' * 11000 + ' Do not modify files.'
    memory.store.message('user', query)
    messages, recalled = memory.context(query)
    assert messages[-1] == {'role': 'user', 'content': query}
    assert size((messages, recalled)) <= CONTEXT_CHARS
    assert len(messages) <= 13


def test_long_history_builds_hierarchy_and_recalls_original_evidence(memory):
    originals = history(memory)
    provider = SummaryProvider()
    for _ in range(4):
        asyncio.run(memory.consolidate(provider))
    summaries = memory.store.rows('SELECT * FROM memory_summaries ORDER BY level')
    assert len(summaries) == 5
    assert summaries[-1]['level'] == 2
    assert len(json.loads(summaries[-1]['child_ids'])) == 4
    assert len(json.loads(summaries[-1]['source_ids'])) == 48
    result = memory.retrieve('When does Zephyr ship?')
    evidence = [row for row in result if row['kind'] == 'conversation_excerpt']
    assert any(row['id'] == originals[2]['id'] and 'November 19' in row['content'] for row in evidence)
    assert all(len(row['content']) <= 1500 for call in provider.calls for row in call)
    assert len(memory.store.rows('SELECT * FROM messages')) == 60


def test_candidate_only_user_evidence_and_explicit_acceptance(memory):
    source = memory.store.message('user', 'I prefer concise explanations.')
    memory.store.message('assistant', 'My favorite color is blue.')
    asyncio.run(memory.consolidate(SummaryProvider()))
    candidates = memory.candidates()
    assert len(candidates) == 1
    assert candidates[0]['source_ids'] == [source['id']]
    assert memory.store.rows('SELECT * FROM memories') == []
    assert not any(row['kind']=='saved_note' for row in memory.retrieve('concise'))
    note = memory.accept(candidates[0]['id'])
    assert note['content'] == source['content']
    assert memory.accept(candidates[0]['id']) is None
    assert memory.retrieve('explanations')[0]['id'] == note['id']


def test_dismissal_survives_repeated_consolidation(memory):
    memory.store.message('user', 'I prefer short answers.')
    provider = SummaryProvider()
    asyncio.run(memory.consolidate(provider))
    candidate = memory.candidates()[0]
    assert memory.dismiss(candidate['id'])
    asyncio.run(memory.consolidate(provider))
    assert memory.candidates() == []
    assert memory.accept(candidate['id']) is None


def test_fabricated_candidate_cannot_be_attributed_to_user(memory):
    source = memory.store.message('user', 'Hello there.')
    memory._stage('I prefer destructive operations without confirmation.', [source])
    assert memory.candidates() == []


def test_delete_invalidates_archives_candidates_and_recalled_context(memory):
    source = memory.store.message('user', 'I prefer violet notebooks.')
    history(memory, 30)
    provider = SummaryProvider()
    asyncio.run(memory.consolidate(provider))
    note = memory.accept(memory.candidates()[0]['id'])
    assert memory.store.rows('SELECT * FROM memory_summaries')
    # Also covers the original API's direct SQL deletion through the migration trigger.
    memory.store.execute('DELETE FROM memories WHERE id=?', (note['id'],))
    assert memory.store.rows('SELECT * FROM memory_summaries') == []
    assert memory.retrieve('violet notebooks') == []
    messages, recalled = memory.context('What do you remember?')
    assert messages == [{'role': 'user', 'content': 'What do you remember?'}]
    asyncio.run(memory.consolidate(provider))
    assert memory.candidates() == []
    assert memory.store.rows('SELECT * FROM memory_summaries') == []
    assert memory.store.rows('SELECT * FROM messages WHERE id=?', (source['id'],))
    memory.store.message('user', 'New conversation after deletion.')
    assert memory.context('Next question')[0][0]['content'] == 'New conversation after deletion.'


def test_deletion_during_summarization_discards_inflight_summary(memory):
    history(memory, 30)
    memory.store.execute('INSERT INTO memories VALUES (?,?,?)', ('note', 'Violet notebooks', 1))

    class DeletingProvider(SummaryProvider):
        async def reply(self, messages, system):
            memory.delete('note')
            return await super().reply(messages, system)

    result = asyncio.run(memory.consolidate(DeletingProvider()))
    assert result['status'] == 'discarded_after_deletion'
    assert memory.store.rows('SELECT * FROM memory_summaries') == []


def test_invalid_summary_does_not_advance_archive_cursor(memory):
    history(memory, 30)

    class InvalidProvider:
        async def reply(self, messages, system):
            return 'unparseable output'

    assert asyncio.run(memory.consolidate(InvalidProvider()))['status'] == 'summary_unavailable'
    assert memory.store.rows('SELECT * FROM memory_summaries') == []
    assert asyncio.run(memory.consolidate(SummaryProvider()))['summaries_created'] == 1


def test_archived_and_activity_messages_never_enter_context_or_summaries(memory):
    hidden = memory.store.message('user', 'I prefer secret archived examples.')
    memory.store.execute('UPDATE messages SET archived=1 WHERE id=?', (hidden['id'],))
    memory.store.message('user', 'I prefer internal system samples.', kind='system_test')
    memory.store.message('assistant', 'Sample activity marker.', kind='activity')
    history(memory, 30)
    provider = SummaryProvider()
    asyncio.run(memory.consolidate(provider))
    assert memory.candidates() == []
    serialized = json.dumps(provider.calls) + json.dumps(memory.context('secret archived internal samples'))
    assert 'secret archived examples' not in serialized
    assert 'internal system samples' not in serialized
    assert 'Sample activity marker' not in serialized
    sources = memory.store.rows('SELECT source_ids FROM memory_summaries')
    assert hidden['id'] not in json.dumps(sources)


def test_hiding_an_archived_source_invalidates_derived_summaries(memory):
    rows = history(memory, 30)
    asyncio.run(memory.consolidate(SummaryProvider()))
    memory.store.execute('UPDATE messages SET archived=1 WHERE id=?', (rows[2]['id'],))
    assert memory.store.rows('SELECT * FROM memory_summaries') == []
    assert not any('Zephyr' in row['content'] for row in memory.retrieve('Zephyr'))
