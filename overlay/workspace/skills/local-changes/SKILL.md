---
name: local-changes
description: Potter's own changes to his shipped skills and global rules, kept safe across updates and sent back for the repo. Use when the weekly local-changes job fires, after an update reports files "kept because they were changed here", or the owner says "publish my local changes", "what have you changed", "take the repo's version of ...".
---
# Local changes (kept across updates, sent back to the repo)

Updates never overwrite a shipped file you changed here; the repo's version is put beside it as `<file>.upstream`.
`potter-shipped --report` rebuilds `state/shipped/DRIFT.md` and `drift.json`: each edited file with its diff
against the repo, each file you added under `skills/` or `rules/global/`, and your notes from
`state/shipped/notes.md`. Your edits only reach the repo when the owner has the developer fold them in.

## Weekly job
Read `state/shipped/drift.json`. Nothing edited and nothing new -> output nothing. Otherwise one short message:
`<n> of my shipped files differ from the repo: <paths>. Say "publish my local changes" to send them for the repo.`
Mention any file whose `.upstream` sits beside it: the repo has a newer version too.

## "Publish my local changes" (the owner's request is the approval for this one Notion write)
1. Run `potter-shipped --report`, then read `state/shipped/DRIFT.md`.
2. The page id is in `state/shipped/notion.json` (`{"page_id": ...}`). Missing: ask the owner once which Notion page
   it should live under, create a child page titled "Potter - local changes", and save its id there.
3. Replace the page's content with DRIFT.md under a dated heading. Never include keys, tokens or vault values.
4. Reply: the page link and the list of files. The owner then tells the developer "sync Potter's changes".

## After the repo catches up
An update where a file now matches the repo clears it by itself. When the repo took the idea in a different form
(for example your specifics moved to your own rules file), check that your own file holds what matters, then run
`potter-shipped --accept <path>` (a file, or a skill folder such as `skills/local-monitor`): the repo's version is
installed and yours is archived under `state/shipped/archive/`. Tell the owner what you accepted.

## Never
Delete or hand-edit a `.upstream` file to make a difference disappear; edit `state/shipped/manifest.json`; or accept
the repo's version without first checking that nothing the owner wanted is lost.
