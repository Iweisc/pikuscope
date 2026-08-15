# Showcase: pikuscope on real t3code PRs

All outputs below are real artifacts from the benchmark (dry-run; nothing was posted to the upstream repo).

## 1. Full review of a merged PR

# pikuscope review — pingdotgg/t3code#2757 (fix(ux): spamming cmd+, no longer stacks settings)

<!-- pikuscope-summary -->
## Summary by pikuscope

### Bug Fixes
- Prevents repeated desktop `open-settings` actions, including Cmd+,, from stacking `/settings` entries in browser history.
- Uses route replacement when the current pathname starts with `/settings`, preserving a single-step Back/Escape path out of settings.

### Refactor
- Reads the active pathname with TanStack Router's `useLocation` and refreshes the menu-action subscription when it changes.

---
<sub>🔬 Review by **pikuscope** · comment `@pikuscope help` for commands</sub>

## Findings (3)

### `apps/web/src/components/AppSidebarLayout.tsx:48`
**🔧 Minor** · `correctness` · **Restrict replacement to the actual settings route tree**

`replace: pathname.startsWith("/settings")` also matches unrelated routes such as `/settings-remote/thread-1`. The dynamic `/$environmentId/$threadId` route and the `EnvironmentId` contract both permit that concrete pathname, so invoking Settings from such a thread replaces the thread's history entry instead of pushing settings. Escape/Back then skips the thread the user came from. Match only `/settings` or a slash-delimited descendant, e.g. `/^\/settings(?:\/|$)/`.

```suggestion
        void navigate({
          to: "/settings",
          replace: /^\/settings(?:\/|$)/.test(pathname),
        });
```

<details>
<summary>🤖 Prompt for AI agents</summary>

```
In apps/web/src/components/AppSidebarLayout.tsx:48, address this review finding: Restrict replacement to the actual settings route tree. A thread under environment ID `settings-remote` has pathname `/settings-remote/thread-1`; Cmd+, replaces that thread entry, so Back from settings cannot return to it. Apply the fix described above, keeping the surrounding code style, then verify the change compiles and existing behavior is preserved.
```
</details>

<sub>🔬 pikuscope</sub>

### `apps/web/src/components/AppSidebarLayout.tsx:48`
**🔧 Minor** · `bug` · **Make settings navigation idempotent, including before the route commit**

`void navigate({ to: "/settings", replace: ... })` still navigates away from the current settings section, and the `/settings` `beforeLoad` then redirects to `/settings/general`. For example, entering General from a chat and following “View diagnostics” produces `[chat, general, diagnostics]`; Cmd+, then replaces Diagnostics with General, producing `[chat, general, general]`, so Escape or Back appears to do nothing once. There is also a pre-commit race: if two `open-settings` IPC messages arrive before React commits the first location update, both callbacks see the original non-settings pathname and push duplicate entries. Treat an existing or in-flight settings navigation as a no-op, synchronously marking the first accepted action so repeated messages are suppressed before the next render.

```suggestion
        if (!/^\/settings(?:\/|$)/.test(pathname)) {
          void navigate({ to: "/settings" });
        }
```

<details>
<summary>🤖 Prompt for AI agents</summary>

```
In apps/web/src/components/AppSidebarLayout.tsx:48, address this review finding: Make settings navigation idempotent, including before the route commit. From `/settings/general`, open `/settings/diagnostics` and press Cmd+, then Back returns to an indistinguishable `/settings/general` entry instead of leaving settings. Apply the fix described above, keeping the surrounding code style, then verify the change compiles and existing behavior is preserved.
```
</details>

<sub>🔬 pikuscope</sub>

### `apps/web/src/components/AppSidebarLayout.tsx:55`
**🔧 Minor** · `performance` · **Keep the desktop menu listener stable across navigation**

Adding `pathname` to `[navigate, pathname]` tears down and re-registers the Electron IPC listener after every route change. The preload implementation maps each registration to `ipcRenderer.on`/`removeListener`, so ordinary thread and settings-section navigation now causes needless IPC subscription churn. Keep one subscription and read the latest pathname through `useEffectEvent` or a ref; that also provides a place to handle rapid repeated actions atomically.

<details>
<summary>🤖 Prompt for AI agents</summary>

```
In apps/web/src/components/AppSidebarLayout.tsx:55, address this review finding: Keep the desktop menu listener stable across navigation. Moving through ten threads causes ten unnecessary `removeListener`/`on` cycles even though the desktop menu source and action handler are unchanged. Apply the fix described above, keeping the surrounding code style, then verify the change compiles and existing behavior is preserved.
```
</details>

<sub>🔬 pikuscope</sub>


<details><summary>Dropped by verifier (5)</summary>

- `apps/web/src/components/AppSidebarLayout.tsx:48` Require a settings path-segment boundary — This is the same overbroad `startsWith('/settings')` defect as finding 0, with only a different example environment ID. Finding 0 already establishes that such dynamic chat paths are reachable and supplies the valid segment-boundary fix.
- `apps/web/src/components/AppSidebarLayout.tsx:48` Match the settings path on a segment boundary — This repeats finding 0's path-segment-boundary issue. `/settings-work/<threadId>` is another instance of the same valid dynamic chat route being misclassified by `startsWith('/settings')`.
- `apps/web/src/components/AppSidebarLayout.tsx:48` Guard repeated settings opens before navigation commits — This is the same stale render-time pathname race as finding 1: repeated menu events can reuse the pre-settings closure while the first settings navigation and redirect are being committed. Finding 1 retains the issue with an appropriately downgraded severity.
- `apps/web/src/components/AppSidebarLayout.tsx:55` Keep the desktop menu subscription stable across navigation — This describes the same stale pathname callback underlying finding 1, focused specifically on the interval before the pathname-dependent passive effect replaces the IPC listener. A stable listener reading current or optimistic state is the same remediation, so a separate review comment would be redu
- `apps/web/src/components/AppSidebarLayout.tsx:55` Keep the desktop menu subscription stable across navigation — This is an exact duplicate of finding 7's deterministic IPC listener teardown and re-registration on each pathname change.
</details>


## 2. Bot-comment audit (second opinions on other bots)

### PR #1543
- **valid** (96%) — cursor[bot] at `apps/server/src/serverSettings.ts:154`
  - claim: ### `Equal.equals` uses reference equality on plain objects  **Medium Severity**  <!-- DESCRIPTION START --> `Equal.equals` from Effect falls back to `===` reference comparison for
  - audit: `ModelSelection` is a union of `Schema.Struct` values (`packages/contracts/src/orchestration.ts:48-62`), which decode to ordinary objects without Effect's `Equal` protocol. `Equal.equals` only gives structural equality to values implementing that protocol; otherwise distinct objects fail the comparison, and the write path compares a freshly schema-decoded `next` (`serverSettings.ts:327`) against t
### PR #2428
- **false_positive** (99%) — cursor[bot] at `apps/server/src/provider/Layers/CursorProvider.ts:None`
  - claim: ### Capability enrichment lacks fallback unlike model discovery  **Medium Severity**  <!-- DESCRIPTION START --> `discoverCursorModelCapabilitiesViaAcp` calls `discoverCursorModels
  - audit: At the PR head there is no `discoverCursorModelCapabilitiesViaAcp` or separate capability-enrichment path. `discoverCursorModelsViaAcp` directly delegates to `discoverCursorModelsViaListAvailableModels` at lines 558-561 with no `catchCause` fallback, and capabilities are built from each returned model's `configOptions` at lines 370-386; comments at lines 1093-1099 explicitly state that both model 
- **false_positive** (99%) — cursor[bot] at `apps/server/src/provider/Layers/CursorProvider.ts:627`
  - claim: ### Enrichment shrinks model catalog  **Medium Severity**  <!-- DESCRIPTION START --> Background enrichment republishes the snapshot using only models returned from `list_available
  - audit: At the PR head, background enrichment performs no model discovery: `enrichCursorSnapshot` passes the existing snapshot to `enrichProviderSnapshotWithVersionAdvisory` (CursorProvider.ts:1116-1119), which returns `{ ...snapshot, versionAdvisory: ... }` and therefore preserves `snapshot.models` (providerMaintenance.ts:464-473). Cursor model discovery occurs during the status check via `list_available
- **valid** (98%) — cursor[bot] at `apps/server/src/provider/Layers/CursorProvider.ts:561`
  - claim: ### Missing config options discovery fallback  **Medium Severity**  <!-- DESCRIPTION START --> `discoverCursorModelsViaAcp` now only calls `cursor/list_available_models` and no lon
  - audit: `discoverCursorModelsViaListAvailableModels` discards the result of `acp.start()` and unconditionally requests `cursor/list_available_models`; there is no `catch` that reads `started.sessionSetupResult.configOptions` if the extension request or schema decoding fails (lines 547-553). `checkCursorProviderStatus` converts such a failure into `discoveryWarning` while leaving `discoveredModels` empty (
- **valid** (99%) — cursor[bot] at `apps/server/src/provider/Layers/CursorProvider.ts:1114`
  - claim: ### Version advisory enrichment skipped  **Low Severity**  <!-- DESCRIPTION START --> `enrichCursorSnapshot` returns immediately when the provider is disabled or the snapshot is un
  - audit: The guard at CursorProvider.ts:1112-1114 returns before the only call to `enrichProviderSnapshotWithVersionAdvisory` at line 1116. There is no alternate enrichment: Cursor snapshot builders call `buildServerProvider` without its optional `driver`, so providerSnapshot.ts:204-210 does not create an advisory, and `withInstanceIdentity` in CursorDriver.ts:78-85 only adds identity fields. Moreover, the