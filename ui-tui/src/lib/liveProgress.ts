import type { Msg, TodoItem } from '../types.js'

export const countPendingTodos = (todos: readonly TodoItem[]) =>
  todos.filter(todo => todo.status === 'in_progress' || todo.status === 'pending').length

export const isTodoDone = (todos: readonly TodoItem[]) =>
  todos.length > 0 && todos.every(todo => todo.status === 'completed' || todo.status === 'cancelled')

export const isToolShelfMessage = (msg: Msg | undefined) =>
  Boolean(msg?.kind === 'trail' && !msg.text && !msg.thinking?.trim() && msg.tools?.length)

export const canHoldToolShelf = (msg: Msg | undefined) =>
  Boolean(msg?.kind === 'trail' && !msg.text && (msg.thinking?.trim() || msg.tools?.length))

export const mergeToolShelfInto = (target: Msg, source: Msg): Msg => ({
  ...target,
  tools: [...(target.tools ?? []), ...(source.tools ?? [])]
})

const isBarrierMessage = (msg: Msg | undefined) => {
  if (!msg) {
    return true
  }

  // Assistant text, user input, intro/panel rows all terminate the shelf.
  if (msg.kind === 'intro' || msg.kind === 'panel' || msg.kind === 'diff') {
    return true
  }

  if (msg.role && msg.role !== 'system') {
    return true
  }

  if (msg.text) {
    return true
  }

  return false
}

// A tool shelf belongs to the *nearest* holder above it — never to an earlier one.
//
// The holder is the closest trail row that can carry tools: the reasoning segment that just
// closed (a trail with `thinking`), or an adjacent tool shelf. Skipping past a reasoning row to
// reach an older tool-carrying shelf made a whole turn's tools pile up under the FIRST thinking
// block, leaving the later thinking blocks empty — the order on screen stopped matching the order
// the model actually worked in (reason → tool → reason → tool).
export const appendToolShelfMessage = (prev: readonly Msg[], msg: Msg): Msg[] => {
  if (!isToolShelfMessage(msg)) {
    return [...prev, msg]
  }

  for (let index = prev.length - 1; index >= 0; index--) {
    const candidate = prev[index]

    if (isBarrierMessage(candidate)) {
      break
    }

    if (canHoldToolShelf(candidate)) {
      const next = [...prev]

      next[index] = mergeToolShelfInto(candidate!, msg)

      return next
    }
  }

  return [...prev, msg]
}
