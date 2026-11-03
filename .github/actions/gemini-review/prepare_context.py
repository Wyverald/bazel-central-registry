#!/usr/bin/env python3
# Copyright 2026 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Prepares .gemini/context.json for BCR Gemini PR reviews."""

import base64
import copy
import json
import os
import pathlib
import re
import subprocess
import sys
import yaml


def gh_api(endpoint, paginate=False, slurp=False):
    cmd = ["gh", "api"]
    if paginate:
        cmd.append("--paginate")
    if slurp:
        cmd.append("--slurp")
    cmd.append(endpoint)
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def presubmit_changed_beyond_platforms_or_bazel(prev_doc, curr_doc):
    """Matches tools/bcr_validation.py verify_presubmit_yml_change logic."""
    if curr_doc == prev_doc:
        return False
    prev_scrubbed = copy.deepcopy(prev_doc)
    curr_scrubbed = copy.deepcopy(curr_doc)
    for doc in (prev_scrubbed, curr_scrubbed):
        if not isinstance(doc, dict):
            return True
        for node in (doc, doc.setdefault("bcr_test_module", {})):
            if isinstance(node, dict):
                mat = node.setdefault("matrix", {})
                if isinstance(mat, dict):
                    mat["bazel"] = None
                    mat["platform"] = None
    return curr_scrubbed != prev_scrubbed


def main():
    repo = os.environ["REPOSITORY"]
    event_name = os.environ["EVENT_NAME"]
    event_action = os.environ.get("EVENT_ACTION", "")
    pr_number = str(os.environ["PR_NUMBER"]).strip()
    comment_body = os.environ.get("COMMENT_BODY", "").strip()
    input_context = os.environ.get("INPUT_CONTEXT", "").strip()
    github_output = os.environ.get("GITHUB_OUTPUT", "/dev/null")

    pr_info = gh_api(f"repos/{repo}/pulls/{pr_number}")
    pr_state = pr_info.get("state")
    is_draft = bool(pr_info.get("draft"))

    is_manual = event_name in ("workflow_dispatch", "issue_comment") or (
        event_name in ("pull_request", "pull_request_target")
        and event_action == "labeled"
    )
    trigger_type = "manual" if is_manual else "automatic"

    if pr_state != "open" or (is_draft and not is_manual):
        print(
            f"Skipping PR #{pr_number} (state={pr_state}, draft={is_draft}, trigger={trigger_type})"
        )
        with open(github_output, "a", encoding="utf-8") as out:
            out.write("should_run=false\n")
        return 0

    additional_context = input_context
    if not additional_context and comment_body:
        additional_context = re.sub(
            r"^(@bazel-io\s+gemini-review|@gemini-cli\s+/review)\s*",
            "",
            comment_body,
        ).strip()

    pages = gh_api(
        f"repos/{repo}/pulls/{pr_number}/files?per_page=100",
        paginate=True,
        slurp=True,
    )
    pr_files = [item for page in pages for item in page]

    pr_author = (pr_info.get("user") or {}).get("login", "")
    head_sha = (pr_info.get("head") or {}).get("sha", "")

    module_version_re = re.compile(r"^modules/([^/]+)/([^/]+)/(.+)$")
    metadata_re = re.compile(r"^modules/([^/]+)/metadata\.json$")

    modified_module_versions_set = set()
    modified_modules_set = set()
    metadata_changed_modules = set()
    added_metadata_modules = set()
    non_module_files = []
    mutated_existing_version_files = []

    for f in pr_files:
        filename = f.get("filename", "")
        status = f.get("status", "")
        m_ver = module_version_re.match(filename)
        m_meta = metadata_re.match(filename)
        if m_ver:
            mod_name, ver_name, rel_path = m_ver.groups()
            modified_module_versions_set.add((mod_name, ver_name))
            modified_modules_set.add(mod_name)
            main_file = pathlib.Path("modules") / mod_name / ver_name / rel_path
            if status != "added" or (
                event_name != "pull_request" and main_file.exists()
            ):
                mutated_existing_version_files.append(filename)
        elif m_meta:
            mod_name = m_meta.group(1)
            modified_modules_set.add(mod_name)
            metadata_changed_modules.add(mod_name)
            if status == "added":
                added_metadata_modules.add(mod_name)
        else:
            non_module_files.append(filename)

    reasons = []
    if len(pr_files) > 500:
        reasons.append(
            f"PR modifies more than 500 files ({len(pr_files)} files)."
        )

    if non_module_files:
        sample = ", ".join(non_module_files[:5])
        more = (
            f" (+{len(non_module_files) - 5} more)"
            if len(non_module_files) > 5
            else ""
        )
        reasons.append(f"Non-module files modified: {sample}{more}.")

    if not modified_modules_set and not non_module_files:
        reasons.append("No module versions modified in this PR.")

    if mutated_existing_version_files:
        sample = ", ".join(mutated_existing_version_files[:5])
        reasons.append(
            f"Existing published module version files modified/deleted (violates add-only policy): {sample}."
        )

    head_meta_by_module = {}
    modified_module_versions_info = []
    for mod_name in sorted(modified_modules_set):
        main_meta_path = pathlib.Path("modules") / mod_name / "metadata.json"
        is_new_module = (
            mod_name in added_metadata_modules or not main_meta_path.exists()
        )
        main_meta = None
        if not is_new_module:
            try:
                if event_name == "pull_request":
                    base_sha = (pr_info.get("base") or {}).get("sha", "main")
                    meta_resp = gh_api(
                        f"repos/{repo}/contents/modules/{mod_name}/metadata.json?ref={base_sha}"
                    )
                    main_meta = json.loads(
                        base64.b64decode(meta_resp["content"]).decode("utf-8")
                    )
                else:
                    main_meta = json.loads(
                        main_meta_path.read_text(encoding="utf-8")
                    )
            except Exception as e:
                reasons.append(f"Failed to parse {main_meta_path} on main: {e}")

        if is_new_module:
            reasons.append(f"Adds a new module '{mod_name}'.")
        else:
            maintainers = (main_meta or {}).get("maintainers") or []
            gh_maintainers = [m for m in maintainers if m.get("github")]
            if not gh_maintainers:
                reasons.append(
                    f"Module '{mod_name}' has no GitHub maintainers listed in metadata.json on main."
                )
            elif pr_author.lower() == "bazel-io":
                notify_maintainers = [
                    m for m in gh_maintainers if not m.get("do_not_notify")
                ]
                if not notify_maintainers:
                    reasons.append(
                        f"PR opened by @bazel-io for '{mod_name}', whose maintainers all have do_not_notify set."
                    )

        head_meta = None
        if mod_name in metadata_changed_modules:
            try:
                meta_resp = gh_api(
                    f"repos/{repo}/contents/modules/{mod_name}/metadata.json?ref=refs/pull/{pr_number}/head"
                )
                head_meta_raw = base64.b64decode(meta_resp["content"]).decode(
                    "utf-8"
                )
                head_meta = json.loads(head_meta_raw)
                head_meta_by_module[mod_name] = head_meta
            except Exception:
                head_meta = None

            if (
                not is_new_module
                and main_meta is not None
                and head_meta is not None
            ):
                main_copy = dict(main_meta)
                head_copy = dict(head_meta)
                main_copy.pop("versions", None)
                head_copy.pop("versions", None)
                if main_copy != head_copy:
                    changed_keys = sorted(
                        k
                        for k in set(main_copy.keys()) | set(head_copy.keys())
                        if main_copy.get(k) != head_copy.get(k)
                    )
                    reasons.append(
                        f"Sensitive metadata.json changes in 'modules/{mod_name}/metadata.json' outside 'versions': {changed_keys}."
                    )

    for mod_name, ver_name in sorted(modified_module_versions_set):
        main_meta_path = pathlib.Path("modules") / mod_name / "metadata.json"
        is_new_module = (
            mod_name in added_metadata_modules or not main_meta_path.exists()
        )
        main_ver_dir = pathlib.Path("modules") / mod_name / ver_name
        mutates_existing = any(
            f.startswith(f"modules/{mod_name}/{ver_name}/")
            for f in mutated_existing_version_files
        ) or (event_name != "pull_request" and main_ver_dir.exists())

        prev_version = None
        prev_version_path = None
        presubmit_changed = False
        if not is_new_module:
            try:
                head_meta = head_meta_by_module.get(mod_name)
                if head_meta and ver_name in (head_meta.get("versions") or []):
                    versions_list = head_meta["versions"]
                    idx = versions_list.index(ver_name)
                    if idx > 0:
                        prev_version = versions_list[idx - 1]
                if not prev_version:
                    main_meta = json.loads(
                        main_meta_path.read_text(encoding="utf-8")
                    )
                    main_versions = main_meta.get("versions") or []
                    if main_versions:
                        prev_version = main_versions[-1]
                if prev_version:
                    candidate_dir = (
                        pathlib.Path("modules") / mod_name / prev_version
                    )
                    if candidate_dir.exists():
                        prev_version_path = str(candidate_dir)
                        prev_presubmit_path = candidate_dir / "presubmit.yml"
                        if prev_presubmit_path.exists():
                            prev_doc = (
                                yaml.safe_load(
                                    prev_presubmit_path.read_text(
                                        encoding="utf-8"
                                    )
                                )
                                or {}
                            )
                            presubmit_resp = gh_api(
                                f"repos/{repo}/contents/modules/{mod_name}/{ver_name}/presubmit.yml?ref=refs/pull/{pr_number}/head"
                            )
                            curr_raw = base64.b64decode(
                                presubmit_resp["content"]
                            ).decode("utf-8")
                            curr_doc = yaml.safe_load(curr_raw) or {}
                            if presubmit_changed_beyond_platforms_or_bazel(
                                prev_doc, curr_doc
                            ):
                                presubmit_changed = True
                                reasons.append(
                                    f"presubmit.yml in '{mod_name}@{ver_name}' differs from previous version '{prev_version}' beyond matrix.platform/matrix.bazel (requires review before applying 'presubmit-auto-run')."
                                )
            except Exception:
                pass

        modified_module_versions_info.append(
            {
                "module": mod_name,
                "version": ver_name,
                "is_new_module": is_new_module,
                "mutates_existing_version": mutates_existing,
                "previous_version": prev_version,
                "previous_version_path": prev_version_path,
                "presubmit_changed_beyond_platforms_or_bazel": presubmit_changed,
            }
        )

    needs_bcr_maintainer_review = len(reasons) > 0

    context_data = {
        "repository": repo,
        "pull_request_number": int(pr_number),
        "pr_author": pr_author,
        "head_sha": head_sha,
        "trigger_type": trigger_type,
        "additional_context": additional_context,
        "needs_bcr_maintainer_review": needs_bcr_maintainer_review,
        "bcr_maintainer_review_reasons": reasons,
        "modified_module_versions": modified_module_versions_info,
        "non_module_files": non_module_files,
        "mutated_existing_version_files": mutated_existing_version_files,
    }

    os.makedirs(".gemini", exist_ok=True)
    with open(".gemini/context.json", "w", encoding="utf-8") as f:
        json.dump(context_data, f, indent=2)

    print("Generated .gemini/context.json:")
    print(json.dumps(context_data, indent=2))

    with open(github_output, "a", encoding="utf-8") as out:
        out.write("should_run=true\n")
        out.write(f"pr_number={pr_number}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
