# Markdown to Confluence (confluence-markdown-sync)

A GitHub Action that publishes Markdown files from your repository to Confluence Cloud pages. Run it on every push to `main` and Confluence stays in step with the docs in your repo.

It sends each file to the REST API of [Markdown Importer for Confluence](https://yamuno.com/products/markdown-importer-for-confluence), which turns the Markdown into a native Confluence page. The full walkthrough of this setup is in our guide [How to sync docs from GitHub to Confluence automatically](https://yamuno.com/blogs/how-to-sync-github-docs-to-confluence-automatically).

## What it does

- Finds Markdown files in a folder (`docs` by default).
- Strips YAML front matter and picks a page title from the front matter `title:`, the first `# ` heading, or the filename.
- Creates or updates one Confluence page per file, all under one parent page.
- Retries rate limits (429) and server errors (5xx) with backoff.
- Stops with a clear message on an invalid or expired token, an expired license, or a wrong space or parent.
- Writes a table of files, titles and results to the job summary.

## Requirements

- [Markdown Importer for Confluence](https://marketplace.atlassian.com/apps/1231894/markdown-importer-for-confluence-markdown-exporter?hosting=cloud&tab=overview) installed on your Confluence Cloud site, with an active license. The REST API is part of the paid app.
- A Confluence admin to create an API token in the app.
- A Linux or macOS runner (`ubuntu-latest` is fine). The action uses the runner's Python 3 and has no other dependencies.

## Setup

### 1. Create an API token

In Confluence, go to **Confluence Settings > Apps > Markdown Importer for Confluence** and open the **API** tab.

1. Click **Create New Token**, give it a label such as `github-docs-sync`, and choose an expiry.
2. Copy the token. It is shown only once.
3. Copy the endpoint URL from the **API Documentation** section on the same page.

The token acts with the Confluence permissions of the person who created it, so consider creating it from an account that can only write to the docs space.

### 2. Add two repository secrets

In your repository, open **Settings > Secrets and variables > Actions** and add:

| Secret | Value |
| --- | --- |
| `MDI_ENDPOINT` | The endpoint URL from the API tab |
| `MDI_TOKEN` | The API token |

### 3. Find the space and parent page IDs

Open Markdown Importer for Confluence, pick the space in the **Space selector** and the parent page in the **Page selector**. Both show the ID you need. See [Finding space and page IDs](https://yamuno.com/docs/markdown-importer-for-confluence/rest-api/api-reference#finding-space-and-page-ids).

### 4. Add the workflow

Create `.github/workflows/publish-docs.yml`:

```yaml
name: Publish docs to Confluence

on:
  push:
    branches: [main]
    paths: ["docs/**"]
  workflow_dispatch:

concurrency:
  group: confluence-docs
  cancel-in-progress: false

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Yamuno-Software/confluence-markdown-sync@v1
        with:
          endpoint: ${{ secrets.MDI_ENDPOINT }}
          token: ${{ secrets.MDI_TOKEN }}
          space-id: DOCS
          parent-id: "123456789"
          path: docs
```

To check titles before anything is published, run it once with `dry-run: true`.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `endpoint` | yes | | Your site's API endpoint URL. Pass it from a secret. |
| `token` | yes | | Markdown Importer API token. Pass it from a secret. |
| `space-id` | yes | | Confluence space ID or key. |
| `parent-id` | yes | | ID of the page every published page goes under. |
| `path` | no | `docs` | Folder to read files from. |
| `glob` | no | `**/*.md` | Which files inside `path` to publish. |
| `overwrite` | no | `true` | Update an existing page with the same title under the parent. With `false`, existing pages fail with 400. |
| `title-from` | no | `frontmatter` | `frontmatter`, `h1` or `filename`. If the chosen source is missing, it falls back down that list. |
| `dry-run` | no | `false` | Print files and titles without calling the API. `endpoint` and `token` can be empty. |

## Outputs

`created`, `updated`, `skipped` and `failed`: the number of files in each state.

## Page titles

Confluence matches pages by title and parent, so the title decides which page a file updates.

- With `title-from: frontmatter`, a file with `title: Getting started` in its front matter becomes the page "Getting started". Without it, the first `# ` heading is used (headings inside code blocks are ignored), then the filename with `-` and `_` turned into spaces.
- If two files end up with the same title, the action stops before sending anything and lists them.
- Changing a title creates a new page. The old one stays until you delete it.

## Limitations

- **Flat publish.** Every file becomes a child of `parent-id`. Folders are not turned into a page tree, because the API does not return the ID of the page it creates. To split docs across several parents, use the action more than once with different `path` and `parent-id` values.
- **No attachments.** Local images such as `./img/diagram.png` are not uploaded. Use absolute image URLs, or upload attachments with the Confluence REST API.
- **Pages only.** The API does not create blog posts.
- **No deletes.** Removing a file from the repo does not remove its page.
- **Edits in Confluence are overwritten** on the next publish when `overwrite` is `true`.
- **About 1 MB per page**, which is Confluence's page size limit.
- **Every matching file is sent on every run.** With `overwrite: true` that is safe, it just updates unchanged pages again.

## Token expiry

API tokens last at most 30 days. When the token expires, the action fails with a 403 and the message "The token has expired". Create a new token in the app and update the `MDI_TOKEN` secret. A recurring calendar reminder a few days before the expiry date saves a failed run.

## Troubleshooting

| Status | Meaning | What to do |
| --- | --- | --- |
| 400 | Bad request, or the page exists and `overwrite` is `false` | Check the file is valid Markdown and not empty, or set `overwrite: true`. |
| 401 | Missing or invalid token | Check the `MDI_TOKEN` secret has no extra spaces or line breaks. |
| 402 | License expired | The app needs an active license when the token is created. Renew it and create a new token. |
| 403 | Token expired | Create a new token and update the secret. |
| 404 | Space or parent page not found | Check `space-id` and `parent-id`, and that the token's creator can access them. |
| 429, 5xx | Rate limit or server error | Retried automatically up to 3 times. If it keeps failing, re-run the job later. |

More in the [REST API troubleshooting guide](https://yamuno.com/docs/markdown-importer-for-confluence/rest-api/troubleshooting).

## Links

- Guide: [How to sync docs from GitHub to Confluence automatically](https://yamuno.com/blogs/how-to-sync-github-docs-to-confluence-automatically)
- [Markdown Importer REST API docs](https://yamuno.com/docs/markdown-importer-for-confluence/rest-api)
- [Markdown Importer for Confluence on the Atlassian Marketplace](https://marketplace.atlassian.com/apps/1231894/markdown-importer-for-confluence-markdown-exporter?hosting=cloud&tab=overview)
- Support: [Yamuno support portal](https://yamuno.atlassian.net/servicedesk/customer/portals)

## Development

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT. Made by the Yamuno team.
