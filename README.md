# Darling AppDB Database

This repository contains the software metadata and compatibility reports displayed on [appdb.darlinghq.org](https://appdb.darlinghq.org/).

## Submitting an App or Report

- **On the Web**: The simplest way is using the form at [appdb.darlinghq.org](https://appdb.darlinghq.org/). Submissions made on the site are automatically converted into pull requests against this repository.
- **Via Pull Request**: You can also submit PRs directly by adding software, version, and report JSON files under `data/software/`.
- **Using AI Coding Agents**: The `darling-appdb-report` skill in [darling-skills](https://github.com/darlinghq/darling-skills) automates guest testing, screenshot optimization, and PR generation.

All pull requests are checked by automated CI and merged once validated.
