import { describe, expect, it } from 'vitest'

import type { Msg } from '../types.js'

import { appendToolShelfMessage, canHoldToolShelf, isTodoDone } from './liveProgress.js'

describe('isTodoDone', () => {
  it('only treats non-empty all-completed/cancelled lists as done', () => {
    expect(isTodoDone([])).toBe(false)
    expect(isTodoDone([{ content: 'x', id: 'x', status: 'completed' }])).toBe(true)
    expect(isTodoDone([{ content: 'x', id: 'x', status: 'in_progress' }])).toBe(false)
    expect(
      isTodoDone([
        { content: 'x', id: 'x', status: 'completed' },
        { content: 'y', id: 'y', status: 'cancelled' }
      ])
    ).toBe(true)
  })
})

describe('tool shelf helpers', () => {
  it('recognizes contextual thinking shelves as holders', () => {
    expect(canHoldToolShelf({ kind: 'trail', role: 'system', text: '', thinking: 'plan' })).toBe(true)
    expect(canHoldToolShelf({ kind: 'trail', role: 'system', text: '', tools: ['one ✓'] })).toBe(true)
    expect(canHoldToolShelf({ role: 'assistant', text: 'done' })).toBe(false)
  })
})

describe('appendToolShelfMessage', () => {
  it('merges adjacent tool shelves into one contextual shelf', () => {
    const merged = appendToolShelfMessage([{ kind: 'trail', role: 'system', text: '', tools: ['one ✓'] }], {
      kind: 'trail',
      role: 'system',
      text: '',
      tools: ['two ✓']
    })

    expect(merged).toEqual([{ kind: 'trail', role: 'system', text: '', tools: ['one ✓', 'two ✓'] }])
  })

  it('adds tools to the nearest contextual thinking shelf', () => {
    const merged = appendToolShelfMessage(
      [{ kind: 'trail', role: 'system', text: '', thinking: 'plan', tools: ['one ✓'] }],
      { kind: 'trail', role: 'system', text: '', tools: ['two ✓'] }
    )

    expect(merged).toEqual([{ kind: 'trail', role: 'system', text: '', thinking: 'plan', tools: ['one ✓', 'two ✓'] }])
  })

  it('attaches to the nearest holder, never to an earlier one', () => {
    const prev: Msg[] = [
      { kind: 'trail', role: 'system', text: '', thinking: 'plan', tools: ['one ✓'] },
      { kind: 'trail', role: 'system', text: '', thinking: 'more plan' }
    ]

    const merged = appendToolShelfMessage(prev, {
      kind: 'trail',
      role: 'system',
      text: '',
      tools: ['two ✓']
    })

    expect(merged).toHaveLength(2)
    expect(merged[0]).toEqual({
      kind: 'trail',
      role: 'system',
      text: '',
      thinking: 'plan',
      tools: ['one ✓']
    })
    expect(merged[1]).toEqual({
      kind: 'trail',
      role: 'system',
      text: '',
      thinking: 'more plan',
      tools: ['two ✓']
    })
  })

  it('keeps each reasoning step with the tools it produced, in order', () => {
    const events: Msg[] = [
      { kind: 'trail', role: 'system', text: '', thinking: 'plan' },
      { kind: 'trail', role: 'system', text: '', tools: ['one ✓'] },
      { kind: 'trail', role: 'system', text: '', thinking: 'more plan' },
      { kind: 'trail', role: 'system', text: '', tools: ['two ✓'] },
      { kind: 'trail', role: 'system', text: '', tools: ['three ✓'] }
    ]

    const reduced = events.reduce<Msg[]>((acc, msg) => appendToolShelfMessage(acc, msg), [])

    // Two steps, two blocks: the later tools never migrate up into the first thinking block.
    expect(reduced).toHaveLength(2)
    expect(reduced[0]).toEqual({
      kind: 'trail',
      role: 'system',
      text: '',
      thinking: 'plan',
      tools: ['one ✓']
    })
    expect(reduced[1]).toEqual({
      kind: 'trail',
      role: 'system',
      text: '',
      thinking: 'more plan',
      tools: ['two ✓', 'three ✓']
    })
  })

  it('gives every reason → tool step its own block across a long turn', () => {
    const events: Msg[] = [
      { kind: 'trail', role: 'system', text: '', thinking: 'step 1' },
      { kind: 'trail', role: 'system', text: '', tools: ['read ✓'] },
      { kind: 'trail', role: 'system', text: '', thinking: 'step 2' },
      { kind: 'trail', role: 'system', text: '', tools: ['skill ✓'] },
      { kind: 'trail', role: 'system', text: '', thinking: 'step 3' },
      { kind: 'trail', role: 'system', text: '', tools: ['patch ✓'] }
    ]

    const reduced = events.reduce<Msg[]>((acc, msg) => appendToolShelfMessage(acc, msg), [])

    expect(reduced.map(msg => [msg.thinking, msg.tools])).toEqual([
      ['step 1', ['read ✓']],
      ['step 2', ['skill ✓']],
      ['step 3', ['patch ✓']]
    ])
  })

  it('starts a new shelf across assistant text boundaries', () => {
    const merged = appendToolShelfMessage(
      [
        { kind: 'trail', role: 'system', text: '', tools: ['one ✓'] },
        { role: 'assistant', text: 'done' }
      ],
      { kind: 'trail', role: 'system', text: '', tools: ['two ✓'] }
    )

    expect(merged).toHaveLength(3)
  })
})
