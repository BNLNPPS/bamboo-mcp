# Deployment notes — OpenSearch read-query tools

## Files changed

```
core/bamboo/llm/opensearch_client.py          NEW
core/bamboo/llm/prompt_log.py                 MODIFIED
core/bamboo/core.py                           MODIFIED
core/bamboo/tools/opensearch_query.py         NEW
core/bamboo/tools/opensearch_promptlog_query.py NEW
core/bamboo/tools/topic_guard.py              MODIFIED
packages/askpanda_atlas/askpanda_atlas/harvester_timeseries_impl.py  MODIFIED
tests/test_opensearch_query.py                NEW
docs/opensearch.md                            MODIFIED
CHANGELOG.md                                  MODIFIED
```

## Install

```bash
pip install -e ./core
```

No new packages required. `opensearch-py` was already needed by existing code.

## IMPORTANT: delete today's daily index

The index template fix (which ensures `@timestamp` is mapped as `date` so
that date-range queries work) only takes effect for indices created *after*
the template is applied. Any index created before this deployment has the
wrong mapping and will return zero results for all date-filtered queries.

Delete the current daily index from OpenSearch so it is recreated correctly
on the next write:

```bash
# Replace the date with today's date
curl -X DELETE \
  "https://os-atlas.cern.ch/os/bamboomcp-promptlog-$(date +%Y.%m.%d)" \
  -u "pilot-monitor-agent:$BAMBOO_OPENSEARCH_PROMPTLOG" \
  -k
```

Or from Python:
```python
import os
from bamboo.llm.opensearch_client import create_os_client
from datetime import datetime, timezone

client = create_os_client(os.environ["BAMBOO_OPENSEARCH_PROMPTLOG"])
index = f"bamboomcp-promptlog-{datetime.now(timezone.utc).strftime('%Y.%m.%d')}"
print(client.indices.delete(index=index))
```

After deletion, restart Bamboo. The template will be applied and the index
recreated on the first logged turn.
