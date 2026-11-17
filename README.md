# Simple Aisle for Home Assistant

Show the stores you choose in [Simple Aisle](https://apps.apple.com/app/id6816798834), the household grocery list for
iPhone and iPad, on Home Assistant's own **Shopping List** and **Local To-do** lists. Add, edit, remove or check off
items on either side, and the other side follows. Checking something off in Home Assistant counts as a purchase in
Simple Aisle.

## Requirements
- Home Assistant 2026.9 or later.
- Simple Aisle 1.2 or later on an iPhone or iPad.
- Your iPhone or iPad must be able to reach Home Assistant:
  - **At home (local network):** Home Assistant's local address works, including plain `http`, for example
    `http://192.168.1.20:8123` or `http://homeassistant.local:8123`.
  - **Away from home:** Home Assistant needs an **`https` address with a valid certificate**, for example Home
    Assistant Cloud, your own reverse proxy, or a VPN such as Tailscale. Simple Aisle refuses plain `http` to
    addresses outside your network.
  - Set both addresses in Home Assistant under **Settings → System → Network**. The pairing code includes them,
    and Simple Aisle tries each one.

## Install
**With HACS:** open HACS → ⋮ → **Custom repositories**, add `https://github.com/ArosemenaApps/simple-aisle-home-assistant`
as an **Integration**, download **Simple Aisle**, and restart Home Assistant.

**Without HACS:** copy the `custom_components/simple_aisle` folder into your Home Assistant `config/custom_components/`
folder and restart Home Assistant.

Then go to **Settings → Devices & services → Add integration → Simple Aisle**.

## Connect Simple Aisle
1. In Home Assistant, as an administrator, open **Simple Aisle** in the sidebar and choose **Pair device**. A code
   appears; it works once, for 10 minutes.
2. In Simple Aisle, open **Settings → Integrations → Home Assistant → Connect Home Assistant** and scan the code.
   The iPhone Camera app works too.
3. Can't scan? Tap **Enter Code Instead** and type the address and code shown under the QR code.
4. Confirm the household. Each iPhone or iPad gets its own key, which you can revoke in the Simple Aisle panel.

## Choose stores
In Simple Aisle, turn on each store you want in Home Assistant. Simple Aisle creates a Home Assistant list for it,
named after the store. To use a list you already have, tap ⓘ next to the store and pick it. Simple Aisle shows what
will happen first, matches items with the same name, and never deletes anything. Your household shares these
choices, so each other phone just connects once. Private stores never sync.

## When changes arrive
- **From Simple Aisle:** right away when a paired device makes the change or opens.
- **From Home Assistant:** within seconds while Simple Aisle is open on a paired device. Otherwise at the next sync,
  every 5 minutes up to once a day, or manually, as chosen in Simple Aisle. iOS decides when a closed app actually
  runs, so treat the interval as a goal.
- Changes made in Home Assistant wait safely, through restarts, until a phone picks them up.
- Home Assistant can't open the app itself. In the integration's options (**Settings → Devices & services →
  Simple Aisle → Configure**), choose a notify service. **Request sync** then sends a notification that opens
  Simple Aisle.

## Extras
- `simple_aisle.move_item` moves an item to another linked store's list. Quantity and note are kept, and no purchase
  is recorded.
- Sensors: *Changes waiting for a phone*, *Paired devices*, *Last sync*. Button: *Request sync*.

## Removing it
In Simple Aisle, choose **Disconnect This Device**. In Home Assistant, remove the integration under **Settings →
Devices & services**, then remove the folder (or uninstall it in HACS) and restart. Your lists and their items stay.

## Privacy
Simple Aisle syncs directly between your devices and your own Home Assistant. Nothing is sent to us or to anyone
else. See [PRIVACY.md](PRIVACY.md) and
[Simple Aisle's privacy policy](https://arosemenaapps.github.io/SimpleAisle-site/privacy.html).

## Contact
Questions or problems: ArosemenaApps@gmail.com. This repository doesn't accept pull requests or issues.

## License
MIT. See [LICENSE](LICENSE).
