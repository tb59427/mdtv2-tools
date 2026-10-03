# Home Assistant -- N.MUSIC

Home Assistant configuration for the Masterlink source N.MUSIC fed by the Pi:
Music Assistant (via Sendspin) and AirPlay, Beo4 control, dashboard card.
**Setup guide: [docs/home-assistant.md](../docs/home-assistant.md).**

| Folder | Contents |
|---|---|
| `automations/` | Automations, one per file, in the format of HA's YAML editor |
| `scripts/` | Scripts, same format |
| `templates/` | State templates of the template sensors (`.jinja`) and the webhook sensor `sensor.mdt_n_music` (goes into `configuration.yaml`) |
| `dashboard/` | The card (needs `custom:button-card` from HACS) |
| `helpers/` | Reference list of the helpers (documentation only) |

Everything except the webhook sensor is created in the HA UI: open the
automation / script / card → *Edit in YAML* → paste. Placeholders are marked
`<…>`. Entity ids and names are German, as in the original setup.
