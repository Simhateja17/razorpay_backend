"""The fictional merchant's product domain.

One Indian consumer-electronics and smart-lifestyle retailer, described in enough
depth that compatibility, cross-sell and merchandising questions have real
answers. Depth matters more than row count (ADR 0025): every line below exists to
make some question answerable, not to inflate a total.

Nothing here is random. The generator walks these tables deterministically, so the
same version and seed always produce the same catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CURRENCY = "INR"

# Capabilities a variant can offer, and that another variant can require. These
# are the only facts `check_compatibility` is allowed to reason from (ADR 0006).
CAPABILITIES: tuple[tuple[str, str, str], ...] = (
    ("cap_connector", "Charging / data connector", "text"),
    ("cap_charge_watts", "Maximum charge output in watts", "numeric"),
    ("cap_bluetooth", "Bluetooth version", "numeric"),
    ("cap_wifi_band", "Wi-Fi band", "text"),
    ("cap_voice_assistant", "Voice assistant support", "text"),
    ("cap_device_model", "Device model the accessory is cut for", "text"),
    ("cap_mount", "Mounting standard", "text"),
)


@dataclass(frozen=True)
class Line:
    """One product line: a family of closely related SKUs."""

    key: str
    name: str
    category: str
    # (low, high) list price in paise, before the generator picks a point in range.
    price_range: tuple[int, int]
    variant_axis: str                     # what distinguishes the variants
    variant_values: tuple[str, ...]
    specs: tuple[tuple[str, object, str | None], ...] = ()   # (key, value, unit)
    provides: tuple[tuple[str, object], ...] = ()            # capability -> value
    requires: tuple[tuple[str, str, object, str], ...] = ()  # (cap, op, value, why)
    pairs_with: tuple[str, ...] = ()      # other line keys this genuinely complements
    blurb: str = ""


CATEGORIES: tuple[tuple[str, str, str | None], ...] = (
    ("cat_audio", "Audio", None),
    ("cat_audio_personal", "Personal Audio", "cat_audio"),
    ("cat_audio_home", "Home Audio", "cat_audio"),
    ("cat_power", "Power & Cables", None),
    ("cat_wearables", "Wearables", None),
    ("cat_smart_home", "Smart Home", None),
    ("cat_computing", "Computing", None),
    ("cat_comfort", "Home Comfort", None),
    ("cat_cases", "Cases & Protection", None),
)

# The house brands. A single merchant with its own labels, not a marketplace.
BRANDS: tuple[str, ...] = ("Aster", "Meridian", "Solace", "Nimbus", "Aldervale", "Kestrel")

# Phone models the accessory lines are cut for. Cases and cables reference these
# by capability, which is what makes "will this fit my phone?" answerable.
DEVICE_MODELS: tuple[str, ...] = (
    "Aster One 12", "Aster One 13", "Aster One 13 Pro", "Meridian Edge 7", "Meridian Edge 8",
)

LINES: tuple[Line, ...] = (
    # ------------------------------------------------------------ personal audio
    Line(
        key="earbuds_core", name="Wireless Earbuds", category="cat_audio_personal",
        price_range=(249900, 899900), variant_axis="colour",
        variant_values=("Graphite", "Ivory", "Deep Teal"),
        specs=(("battery_hours", 28, "h"), ("driver_mm", 11, "mm"), ("anc", True, None),
               ("water_resistance", "IPX4", None)),
        provides=(("cap_bluetooth", 5.3),),
        requires=(("cap_bluetooth", "gte", 5.0,
                   "These earbuds need a source device on Bluetooth 5.0 or newer."),),
        pairs_with=("case_earbud", "charger_fast"),
        blurb="Active noise cancelling earbuds tuned for commutes and calls.",
    ),
    Line(
        key="earbuds_sport", name="Sport Earbuds", category="cat_audio_personal",
        price_range=(179900, 449900), variant_axis="colour",
        variant_values=("Slate", "Signal Orange"),
        specs=(("battery_hours", 22, "h"), ("water_resistance", "IP67", None), ("anc", False, None)),
        provides=(("cap_bluetooth", 5.2),),
        pairs_with=("charger_fast",),
        blurb="Sweat-proof earbuds with a secure-fit wing for running and the gym.",
    ),
    Line(
        key="headphones_over_ear", name="Over-Ear Headphones", category="cat_audio_personal",
        price_range=(699900, 1899900), variant_axis="colour",
        variant_values=("Midnight", "Sandstone"),
        specs=(("battery_hours", 45, "h"), ("driver_mm", 40, "mm"), ("anc", True, None),
               ("weight_g", 254, "g")),
        provides=(("cap_bluetooth", 5.3), ("cap_connector", "usb_c")),
        pairs_with=("cable_usbc", "case_headphone"),
        blurb="Full-size headphones with adaptive noise cancelling for long sessions.",
    ),
    # ---------------------------------------------------------------- home audio
    Line(
        key="speaker_portable", name="Portable Speaker", category="cat_audio_home",
        price_range=(299900, 999900), variant_axis="size",
        variant_values=("Compact", "Standard", "Large"),
        specs=(("battery_hours", 18, "h"), ("water_resistance", "IP67", None),
               ("output_watts", 30, "W")),
        provides=(("cap_bluetooth", 5.2), ("cap_connector", "usb_c")),
        pairs_with=("cable_usbc", "charger_fast"),
        blurb="A rugged Bluetooth speaker that survives a monsoon balcony.",
    ),
    Line(
        key="soundbar", name="Soundbar", category="cat_audio_home",
        price_range=(1299900, 3499900), variant_axis="channels",
        variant_values=("2.1", "3.1", "5.1"),
        specs=(("output_watts", 240, "W"), ("hdmi_earc", True, None)),
        provides=(("cap_voice_assistant", "alexa"), ("cap_wifi_band", "5GHz")),
        pairs_with=("smart_speaker",),
        blurb="A living-room soundbar with eARC and a wireless subwoofer.",
    ),
    # ------------------------------------------------------------ power & cables
    Line(
        key="charger_fast", name="Fast Charger", category="cat_power",
        price_range=(89900, 349900), variant_axis="output",
        variant_values=("30W", "65W", "100W"),
        specs=(("ports", 2, None), ("gan", True, None)),
        provides=(("cap_connector", "usb_c"), ("cap_charge_watts", 65)),
        pairs_with=("cable_usbc",),
        blurb="A compact GaN charger that actually fits an Indian wall socket.",
    ),
    Line(
        key="cable_usbc", name="USB-C Cable", category="cat_power",
        price_range=(39900, 149900), variant_axis="length",
        variant_values=("1 m", "2 m"),
        specs=(("braided", True, None), ("data_gbps", 10, "Gbps")),
        provides=(("cap_connector", "usb_c"),),
        requires=(("cap_charge_watts", "gte", 30,
                   "This cable is rated for 30W and above; a lower-output charger will not reach its rated speed."),),
        blurb="A braided 240W-rated USB-C cable with real data throughput.",
    ),
    Line(
        key="power_bank", name="Power Bank", category="cat_power",
        price_range=(199900, 699900), variant_axis="capacity",
        variant_values=("10000 mAh", "20000 mAh"),
        specs=(("capacity_mah", 20000, "mAh"), ("passthrough", True, None)),
        provides=(("cap_connector", "usb_c"), ("cap_charge_watts", 45)),
        pairs_with=("cable_usbc",),
        blurb="Airline-legal capacity with enough output to charge a laptop slowly.",
    ),
    # ------------------------------------------------------------------ wearables
    Line(
        key="smartwatch", name="Smartwatch", category="cat_wearables",
        price_range=(899900, 2999900), variant_axis="case",
        variant_values=("41 mm", "45 mm"),
        specs=(("battery_hours", 72, "h"), ("amoled", True, None), ("gps", True, None),
               ("water_resistance", "5ATM", None)),
        provides=(("cap_bluetooth", 5.3),),
        requires=(("cap_bluetooth", "gte", 5.0,
                   "The watch pairs over Bluetooth 5.0 or newer."),),
        pairs_with=("watch_strap", "charger_fast"),
        blurb="A everyday smartwatch with genuine multi-day battery life.",
    ),
    Line(
        key="fitness_band", name="Fitness Band", category="cat_wearables",
        price_range=(199900, 599900), variant_axis="colour",
        variant_values=("Black", "Coral"),
        specs=(("battery_hours", 240, "h"), ("spo2", True, None)),
        provides=(("cap_bluetooth", 5.1),),
        blurb="A lightweight band for step, sleep and heart-rate tracking.",
    ),
    Line(
        key="watch_strap", name="Watch Strap", category="cat_wearables",
        price_range=(49900, 249900), variant_axis="material",
        variant_values=("Silicone", "Woven", "Leather"),
        specs=(("quick_release", True, None),),
        requires=(("cap_mount", "eq", "watch_22mm",
                   "This strap fits a 22 mm quick-release lug only."),),
        blurb="A quick-release strap in the sizes the watches actually use.",
    ),
    # ---------------------------------------------------------------- smart home
    Line(
        key="smart_bulb", name="Smart Bulb", category="cat_smart_home",
        price_range=(49900, 199900), variant_axis="type",
        variant_values=("White", "Colour"),
        specs=(("lumens", 900, "lm"), ("dimmable", True, None)),
        provides=(("cap_wifi_band", "2.4GHz"), ("cap_voice_assistant", "google")),
        requires=(("cap_wifi_band", "eq", "2.4GHz",
                   "Smart bulbs join a 2.4 GHz network; they cannot see a 5 GHz-only band."),),
        pairs_with=("smart_speaker",),
        blurb="A Wi-Fi bulb that works without a hub.",
    ),
    Line(
        key="smart_plug", name="Smart Plug", category="cat_smart_home",
        price_range=(59900, 149900), variant_axis="rating",
        variant_values=("6 A", "16 A"),
        specs=(("energy_monitoring", True, None),),
        provides=(("cap_wifi_band", "2.4GHz"), ("cap_voice_assistant", "alexa")),
        requires=(("cap_wifi_band", "eq", "2.4GHz",
                   "Smart plugs join a 2.4 GHz network."),),
        blurb="A 16 A plug rated for a geyser or an air conditioner.",
    ),
    Line(
        key="security_camera", name="Security Camera", category="cat_smart_home",
        price_range=(249900, 899900), variant_axis="placement",
        variant_values=("Indoor", "Outdoor"),
        specs=(("resolution_p", 2160, "p"), ("night_vision", True, None),
               ("local_storage", True, None)),
        provides=(("cap_wifi_band", "2.4GHz"),),
        requires=(("cap_wifi_band", "eq", "2.4GHz",
                   "The camera pairs on 2.4 GHz; a 5 GHz-only router cannot complete setup."),),
        blurb="A 4K camera with local microSD recording, no subscription required.",
    ),
    Line(
        key="smart_speaker", name="Smart Speaker", category="cat_smart_home",
        price_range=(299900, 899900), variant_axis="size",
        variant_values=("Mini", "Standard"),
        specs=(("output_watts", 20, "W"), ("far_field_mic", True, None)),
        provides=(("cap_voice_assistant", "alexa"), ("cap_wifi_band", "5GHz")),
        blurb="A voice speaker that can drive the rest of the smart-home range.",
    ),
    # ----------------------------------------------------------------- computing
    Line(
        key="keyboard_mech", name="Mechanical Keyboard", category="cat_computing",
        price_range=(449900, 1299900), variant_axis="layout",
        variant_values=("65%", "TKL", "Full"),
        specs=(("hot_swap", True, None), ("switch", "tactile", None)),
        provides=(("cap_bluetooth", 5.1), ("cap_connector", "usb_c")),
        pairs_with=("cable_usbc", "mouse_wireless"),
        blurb="A hot-swappable board that survives a full working day of typing.",
    ),
    Line(
        key="mouse_wireless", name="Wireless Mouse", category="cat_computing",
        price_range=(149900, 699900), variant_axis="colour",
        variant_values=("Graphite", "Ivory"),
        specs=(("dpi", 26000, "dpi"), ("battery_hours", 90, "h")),
        provides=(("cap_bluetooth", 5.1),),
        blurb="A low-latency mouse that pairs to three machines at once.",
    ),
    Line(
        key="monitor", name="Monitor", category="cat_computing",
        price_range=(1499900, 4999900), variant_axis="size",
        variant_values=("24 in", "27 in", "32 in"),
        specs=(("refresh_hz", 120, "Hz"), ("resolution_p", 1440, "p"), ("usb_c_pd", True, None)),
        provides=(("cap_connector", "usb_c"), ("cap_charge_watts", 90), ("cap_mount", "vesa_100")),
        pairs_with=("dock_usbc", "monitor_arm"),
        blurb="A colour-accurate panel that charges a laptop over the same cable.",
    ),
    Line(
        key="dock_usbc", name="USB-C Dock", category="cat_computing",
        price_range=(699900, 1999900), variant_axis="ports",
        variant_values=("8-in-1", "11-in-1"),
        specs=(("hdmi_outputs", 2, None), ("ethernet_gbps", 1, "Gbps")),
        provides=(("cap_connector", "usb_c"),),
        requires=(("cap_charge_watts", "gte", 65,
                   "The dock passes power through to your laptop and needs a 65W or larger charger."),),
        pairs_with=("charger_fast", "cable_usbc"),
        blurb="A dock that drives two displays and still charges the laptop.",
    ),
    Line(
        key="monitor_arm", name="Monitor Arm", category="cat_computing",
        price_range=(299900, 899900), variant_axis="reach",
        variant_values=("Single", "Dual"),
        specs=(("max_load_kg", 9, "kg"),),
        requires=(("cap_mount", "eq", "vesa_100",
                   "The arm mounts to a VESA 100 × 100 pattern."),),
        pairs_with=("monitor",),
        blurb="A gas-spring arm that clears the desk under the display.",
    ),
    Line(
        key="ssd_portable", name="Portable SSD", category="cat_computing",
        price_range=(549900, 1899900), variant_axis="capacity",
        variant_values=("1 TB", "2 TB"),
        specs=(("read_mbps", 1050, "MB/s"), ("shock_rated", True, None)),
        provides=(("cap_connector", "usb_c"),),
        pairs_with=("cable_usbc",),
        blurb="Pocket storage fast enough to edit straight off the drive.",
    ),
    # ------------------------------------------------------------- home comfort
    Line(
        key="air_purifier", name="Air Purifier", category="cat_comfort",
        price_range=(899900, 2999900), variant_axis="coverage",
        variant_values=("Room", "Living"),
        specs=(("cadr", 400, "m3/h"), ("hepa", True, None), ("noise_db", 24, "dB")),
        provides=(("cap_wifi_band", "2.4GHz"), ("cap_voice_assistant", "google")),
        pairs_with=("purifier_filter",),
        blurb="True-HEPA filtration sized honestly for Indian room dimensions.",
    ),
    Line(
        key="purifier_filter", name="Purifier Filter", category="cat_comfort",
        price_range=(129900, 349900), variant_axis="type",
        variant_values=("HEPA", "HEPA + Carbon"),
        specs=(("life_months", 9, "months"),),
        requires=(("cap_mount", "eq", "filter_r400",
                   "This cartridge fits the R400 purifier body only."),),
        blurb="The replacement cartridge, sold at a sane price.",
    ),
    # ---------------------------------------------------------------- protection
    Line(
        key="case_phone", name="Phone Case", category="cat_cases",
        price_range=(49900, 249900), variant_axis="finish",
        variant_values=("Clear", "Frosted", "Leather"),
        specs=(("drop_rated_m", 3, "m"), ("magsafe", True, None)),
        requires=(("cap_device_model", "eq", None,
                   "A case is cut for one phone model and will not fit another."),),
        blurb="A drop-rated case cut for one specific handset.",
    ),
    Line(
        key="case_earbud", name="Earbud Case", category="cat_cases",
        price_range=(29900, 99900), variant_axis="finish",
        variant_values=("Silicone", "Woven"),
        specs=(("carabiner", True, None),),
        blurb="A silicone shell so the buds survive a bag.",
    ),
    Line(
        key="case_headphone", name="Headphone Case", category="cat_cases",
        price_range=(99900, 299900), variant_axis="finish",
        variant_values=("Hard Shell", "Fabric"),
        specs=(("water_resistant", True, None),),
        blurb="A moulded case for over-ear headphones and their cable.",
    ),
    Line(
        key="screen_protector", name="Screen Protector", category="cat_cases",
        price_range=(19900, 99900), variant_axis="pack",
        variant_values=("Single", "Twin Pack"),
        specs=(("hardness", "9H", None), ("oleophobic", True, None)),
        requires=(("cap_device_model", "eq", None,
                   "A protector is cut for one phone model."),),
        blurb="Tempered glass with an alignment frame that actually works.",
    ),
)

# Product-line adjectives, walked deterministically to give each SKU a distinct
# name without a random word soup.
EDITIONS: tuple[str, ...] = (
    "Core", "Studio", "Pro", "Air", "Max", "Lite", "Everyday", "Signature",
    "Field", "Metro", "Halo", "Atlas", "Aurora", "Quartz", "Slate", "Lumen",
)

LOCATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("loc_blr", "BLR", "Bengaluru fulfilment centre", "South"),
    ("loc_del", "DEL", "Delhi NCR fulfilment centre", "North"),
    ("loc_mum", "MUM", "Mumbai fulfilment centre", "West"),
)

# Given names and surnames for seeded customers. Indian-market appropriate, and
# fixed so a reset reproduces the same people.
GIVEN_NAMES: tuple[str, ...] = (
    "Ira", "Dev", "Anaya", "Kabir", "Meera", "Rohan", "Sana", "Vikram", "Priya", "Arjun",
    "Nikhil", "Tara", "Aditya", "Kavya", "Rahul", "Divya", "Farhan", "Neha", "Siddharth", "Riya",
)
SURNAMES: tuple[str, ...] = (
    "Menon", "Rao", "Sharma", "Iyer", "Banerjee", "Chawla", "Nair", "Kulkarni", "Desai", "Bose",
)

FULFILMENT_OPTIONS: tuple[tuple[str, int, int], ...] = (
    # (option, shipping in paise, promised days)
    ("standard", 0, 4),
    ("express", 9900, 2),
)

PROMOTIONS: tuple[tuple[str, str, str, int, int, str | None], ...] = (
    # (code, description, kind, value, min subtotal in paise, category scope)
    # A promotion has to be enforceable exactly as its description reads: the
    # category is what confines the audio and smart-home offers to the aisles they
    # name, and a null scope is genuinely storewide.
    ("MONSOON10", "Monsoon sale: ₹200 off orders above ₹2,000", "fixed_minor", 20000,
     200000, None),
    ("AUDIO500", "₹500 off personal audio above ₹5,000", "fixed_minor", 50000,
     500000, "cat_audio_personal"),
    ("SMARTHOME15", "15% off smart home above ₹3,000", "percentage", 15,
     300000, "cat_smart_home"),
)

CAMPAIGNS: tuple[tuple[str, str, str, int], ...] = (
    # (name, channel, promotion code, budget in paise)
    ("Monsoon Audio Push", "search", "AUDIO500", 15000000),
    ("Smart Home Diwali", "social", "SMARTHOME15", 25000000),
    ("Always-On Brand", "display", "MONSOON10", 8000000),
)


@dataclass
class Counts:
    """What one generator run produced, for the reset acceptance check."""

    values: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        self.values[key] = self.values.get(key, 0) + n

    def as_dict(self) -> dict[str, int]:
        return dict(sorted(self.values.items()))
