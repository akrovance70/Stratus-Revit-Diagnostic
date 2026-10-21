# Stratus Revit Diagnostic (V5.3)

A read-only **pyRevit / Revit 2026** diagnostic for investigating model content when Autodesk translation fails during Stratus publishing. This is an **unofficial community tool** and is not affiliated with or endorsed by Autodesk or Stratus.

## What it does

- Inventories family/type names, instance counts, unusual characters, family opening results, and nested/imported content.
- Collects fabrication pipework and supports sampling 250, 1,500, or all parts.
- Inspects nested Revit geometry, with safeguards and reports of local API exceptions.
- Inventories Revit links, coordination models, DirectShapes, categories, and relevant element IDs.
- Exports timestamped CSVs, an HTML **Executive Summary**, and a text summary.

**Limitations:** A clean local scan does **not** prove Autodesk cloud translation will succeed. Geometry tests are sampling-based unless the full option is selected. The script has been tested in a specific Revit 2026 environment, but has not been broadly validated across multiple installations.

## Installation

1. Install pyRevit with Revit 2026 support.
2. Copy `Revit_Stratus_Diagnostic.pushbutton` into the `.panel` folder of your registered pyRevit extension under a `.tab` folder.
3. Reload pyRevit.
4. Open the model and choose **Stratus Diagnostic V5.3**; follow the scan prompts.
5. Find reports in `~/Desktop/Revit_Stratus_Diagnostics`, with a Documents fallback.

Run on a disposable copy when performing subsequent isolation tests. The diagnostic itself is designed to be read-only.

## Privacy and safe sharing

Reports can include confidential model and project names, link paths, instance metadata, element IDs, and other project information. **Do not commit diagnostic exports or project models to a public repository.** Review source and any sample content for internal details before sharing. `.gitignore` does not remove already-tracked files.

## Open source license

A `LICENSE.template` is included, **not an active license**. Confirm permission and the correct legal copyright holder, then replace `[INSERT APPROVED COPYRIGHT HOLDER]` and rename it to `LICENSE` before public release. Until then, don't describe this repository as MIT-licensed.

## Contributions

Bug reports and pull requests are welcome. Please use anonymized examples; do not attach confidential Revit models or production log exports.
