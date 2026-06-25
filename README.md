# ecoscope-workflows-ext-gcf

GCF-specific custom tasks for Ecoscope Desktop workflows, registered with
`wt-registry` so they can be referenced from any module's `spec.yaml`.

## Usage

Add this package as a `git` PyPI requirement in a module's `spec.yaml` —
no conda channel or publishing step required:

```yaml
requirements:
  - name: python
    version: "3.12.*"
  - name: ecoscope-workflows-ext-gcf
    git: https://github.com/Giraffe-Conservation-Foundation/ecoscope-workflows-ext-gcf.git
    tag: v0.1.0
```

**Important:** any `spec.yaml` using a `git`/`path`/`url` (PyPI-style) requirement
must also pin a `python` version, as shown above. Without it, `wt-compiler`'s
ephemeral task-discovery environment can resolve Python 3.14+, where
`pydantic-core` and other transitive deps lack prebuilt wheels — this silently
breaks registration of a scattered subset of built-in `ecoscope-platform`
tasks (not just this package's own task), producing confusing
"not a registered known task name" errors for unrelated tasks. See the
[Ecoscope Platform SDK troubleshooting guide](https://ecoscope.io/en/latest/platform-sdk/troubleshooting/#developing-against-a-local-ecoscope-checkout).

Then reference any task in this package by its registered name:

```yaml
workflow:
  - name: Flatten GCF Repeat Groups
    id: flatten_repeat_groups
    task: flatten_gcf_repeat_groups
    partial:
      df: ${{ workflow.convert_event_details_timezone.return }}
```

## Tasks

### `flatten_gcf_repeat_groups`

Forward-fills parent event metadata onto orphan child rows produced by
EarthRanger repeat groups, drops superseded parent rows, and explodes
`detail_*` list-of-dict columns into one row per repeat-group entry with
flattened sub-fields.

See `src/ecoscope_workflows_ext_gcf/__init__.py` for full implementation
notes.

## Versioning

Tag a release (`git tag v0.1.0 && git push --tags`) any time the task
signatures change, so consuming modules can pin to a specific version in
their `requirements` block.

## Local development

To test changes against a module before tagging a release, point the
module's `spec.yaml` at a local editable path instead of `git`:

```yaml
requirements:
  - name: ecoscope-workflows-ext-gcf
    path: /absolute/path/to/ecoscope-workflows-ext-gcf
    editable: true
```
