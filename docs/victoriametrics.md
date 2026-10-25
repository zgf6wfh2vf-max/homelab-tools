# VictoriaMetrics notes

The tools write with the **InfluxDB line protocol** to `<VM_URL>/write`. VictoriaMetrics names the
resulting series `<measurement>_<field>`, and turns tags into labels:

| Written | Becomes |
|---|---|
| `solis pv_w=512` | `solis_pv_w` |
| `tado,zone=Upstairs inside_c=21.4` | `tado_inside_c{zone="Upstairs"}` |

## Two settings that matter

**De-duplication.** `octopus-collector` re-fetches recent days on every run, and `podpoint-monitor`
backfills history, so the same sample (same series, same timestamp) is sent more than once. Start
VictoriaMetrics with

```
-dedup.minScrapeInterval=1ms
```

so identical timestamps collapse into one sample. Without it, daily sums are inflated.

**Query cache after backdated writes.** VictoriaMetrics caches query results for past time ranges.
Tools that write samples with old timestamps call `/internal/resetRollupResultCache` afterwards
(see `VictoriaMetrics.reset_cache`). If you load data by hand, do the same - otherwise Grafana keeps
showing the old, incomplete numbers.

## Retention and size

`-retentionPeriod=10y` is fine for a home: every tool together writes a few MB per month.
