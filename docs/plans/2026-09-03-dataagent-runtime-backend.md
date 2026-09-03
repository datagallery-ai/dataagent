# DataAgent Runtime Backend Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the repository's runnable Deep Agents example backend with the native `runtime/dataagent` implementation while preserving the existing frontend AG-UI contract.

**Architecture:** `apps/api` owns authentication, AG-UI transport, and durable SQLite lifecycle. It constructs `DataAgent` graphs from one compatible YAML configuration per authenticated user and client thread, injects shared checkpointer/store instances, and namespaces persistence by user. `runtime/dataagent` owns all model, tool, middleware, workspace, skill, and subagent compilation.

**Tech Stack:** Python 3.11, FastAPI, LangGraph, Deep Agents 0.7.5, AG-UI LangGraph, SQLite, uv.

## Tasks

1. Preserve the existing native Deep Agents refactor in an independent commit.
2. Base the integration branch on official `deepagents_refactor` and migrate that commit without rewriting it.
3. Remove the placeholder `runtime/deepagents` package and point `apps/api` at editable `runtime/dataagent`.
4. Extend `DataAgent` graph construction to accept application-owned checkpointer and store instances.
5. Add an API runtime service that creates/caches graphs by authenticated user and AG-UI thread and prevents cross-user checkpoint collisions.
6. Translate DataAgent human-feedback interrupts into the existing AG-UI `ask_user` contract and preserve resume commands.
7. Verify dependency resolution, focused runtime/API tests, a real DeepSeek streaming run, and repository lint/format checks.

## Acceptance

- `apps/api` imports no code from `runtime/deepagents` and resolves `deepagents==0.7.5` through `runtime/dataagent`.
- The same client thread ID used by two users maps to distinct workspaces and checkpoint namespaces.
- Checkpoints and store data survive API process restart through SQLite.
- Standard AG-UI streaming, human-feedback interrupt, and resume payloads remain consumable by the unchanged frontend.
- Runtime and API focused tests pass; agent end-to-end validation uses the configured DeepSeek endpoint rather than a fake model.
