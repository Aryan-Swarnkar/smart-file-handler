# O — Privacy & data lifecycle (P3 slice; grows with P7/P9)

**Core skill**: users own their data; deletion means deletion; foreign data
is invisible, never merely "forbidden".

## Ownership = 404, never 403 (P3)

`FileService.get_file` (`app/services/files.py`) returns `NotFoundError`
whenever a row is missing **or** belongs to another user — every files route
funnels through it. Consequences:

- An attacker cannot distinguish "does user X have file Y?" from "no such
  file" (probing oracle eliminated — same principle as P2's generic 401).
- There is no 403 surface that leaks the existence of another user's object.
- Tests: `tests/test_files.py::test_cross_user_access_is_404` exercises
  GET/complete/PATCH/DELETE/url with an intruder token.

## Hard delete (P3, user-locked decision)

`DELETE /files/{id}` removes BOTH the metadata row and the S3 object —
there is no soft-delete/trash tombstone in P3:

- Order matters: **S3 object first, DB row second**. A failed S3 delete
  leaves the row intact (client retries; S3 delete is idempotent), while the
  reverse would commit a row pointing at a deleted object forever.
- The presigned view URL dies with the object — after delete, the old URL
  returns 404/403 from S3 (verified in
  `test_delete_removes_row_and_object`).
- FK design (P1) already cascades: deleting a user deletes their
  files/labels; deleting a file will cascade its `face_instances` and
  `file_labels` rows (P5/P6 wire the ML rows; the FKs exist since P1).

## Presigned URLs as access control (P3)

No public bucket, no anonymous ACLs: every byte transfer happens through a
short-lived (15 min) signed URL minted only for the owner. Sharing a URL is
time-boxed by construction; revocation = hard delete.

## Still to come

- P7: person/label curation — unassign/merge must preserve user consent
  semantics (faces belong to a person the USER says they belong to).
- P9: GDPR-style account deletion sweep (all S3 objects of a user), audit
  logging of admin access, X-Forwarded-For trust boundary.
