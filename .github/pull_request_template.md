## What and why

<!-- What changes, and what problem it solves. Link the issue if there is one. -->

## How it was checked

<!-- Commands you ran, hardware you ran them on, and what the output was. -->

- [ ] `make lint` and `make test` are green
- [ ] New user-facing behaviour has a test, in this pull request
- [ ] Docs updated (and `make docs` re-run if the settings model changed)
- [ ] No new runtime dependency
- [ ] No default changed in a way that could delete somebody's photo
- [ ] Every string I added is in English

## Anything touching the pipeline?

<!-- pipeline.py, the sanity gate and the delete verification chain were verified against a
     live Immich v3.1.0 instance and several of their oddities are fixes for reproduced
     bugs. If this changes one of them, say which test proves the new behaviour. Delete this
     section if it does not. -->
