# Security and operator responsibilities

Media Intake automates a step that is sensitive in three separate ways. None of
them are solved by this tool; all of them are made visible by it.

## 1. Authorisation

The tool can only tell whether a request *worked*. It cannot tell whether copying
the content was permitted. That judgement belongs to the operator and is recorded in
`source.md` as an uncertainty note, not as a clearance.

- Your own saved folder: you can see it; that is not the same as a right to
  redistribute it.
- Someone else's public posts: publicly visible is not public domain.
- Private accounts: requires that account's session. Holding a session is not
  permission, and using it this way may breach the platform's terms and the
  account holder's expectations.

## 2. Credentials

Instagram cookies are full-account credentials.

- Never write cookies into a project directory, a manifest, a commit, or a log.
- Prefer `--cookies-from-browser`, which reads the browser's existing session in
  place.
- If you must use `--cookies <file>`, keep that file outside any repository and
  delete it when done. Filesystem permissions do not protect against other
  processes running as the same user.

## 3. Rate limits and access controls

Defaults are deliberately slow: `--sleep-request 3.0-6.0`, `--sleep-429 60-120`,
`retries 4`. These exist to keep folder-scale reads inside what the platform
tolerates.

Do not tune them to "finish faster". A 429 means stop and wait; repeated retries
against a throttle turn a temporary block into an account-level restriction. Never
add proxy rotation, session rotation, or other circumvention — if the platform is
refusing the request, that refusal is the answer, not an obstacle to engineer
around.

## 4. Data handling

- Media and metadata land in `_archive/` and `_inbox/` on local disk. Nothing is
  uploaded anywhere by this tool.
- `intake-manifest.json` contains captions, handles, and post URLs. Treat it as
  personal data when the creator is a private individual.
- Deleting the inbox does not delete the archive; clear both when a collection is
  no longer wanted, and remember the download archive is what makes re-runs cheap.

## 5. What this tool does not do

- It does not verify facts, authorship, or rights.
- It does not OCR. That is Media OCR's job, and its output is a review draft.
- It does not guarantee completeness. A collection can shrink, a post can be
  deleted, and a session may see less than a browser does. Compare
  `posts_in_archive` against the plan, and say so when numbers disagree.

## Reporting problems

If gallery-dl's output shape changes (a version bump), the sidecar parser here may
silently stage nothing. `plan` and `fetch` both surface `errors[]`; if a run stages
zero posts with no errors, investigate before assuming the target was empty.
