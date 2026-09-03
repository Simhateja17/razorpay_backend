from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from marketplace_backend.store import Store


CATEGORIES = {
    "Electronics": (["Aster", "Nimbus", "Orbit", "Arc", "Pixel", "Nova"], [
        ("Wireless Earbuds", "Bluetooth earbuds with charging case", 1299),
        ("Fast Charger", "USB-C PD compact wall charger", 699),
        ("Power Bank", "Fast-charging portable battery", 1099),
        ("Smart Watch", "Fitness tracking smart watch", 2499),
        ("Bluetooth Speaker", "Portable wireless speaker", 1499),
        ("USB-C Cable", "Braided fast-charge cable", 299),
        ("Mechanical Keyboard", "Tactile compact mechanical keyboard", 2199),
        ("Wireless Mouse", "Ergonomic rechargeable mouse", 799),
    ]),
    "Home & Kitchen": (["Solace", "Ferro", "Morrow", "Hearth", "Willow", "Terra"], [
        ("Coffee Maker", "Compact drip coffee maker", 2199),
        ("Cast-Iron Pan", "Pre-seasoned cast-iron cookware", 1299),
        ("Storage Set", "Leak-proof food storage containers", 899),
        ("Mixer Grinder", "Multi-jar kitchen mixer grinder", 2799),
        ("Electric Kettle", "Automatic shut-off electric kettle", 999),
        ("Table Lamp", "Adjustable warm-white desk lamp", 749),
        ("Bedsheet Set", "Soft cotton double bedsheet set", 999),
        ("Water Bottle", "Insulated stainless-steel bottle", 599),
    ]),
    "Fashion": (["Meridian", "Aldervale", "Canvas", "Drift", "Sage", "North"], [
        ("Running Shoes", "Breathable cushioned running shoes", 1999),
        ("Cotton T-Shirt", "Comfortable cotton crew-neck T-shirt", 499),
        ("Denim Jacket", "Mid-wash everyday denim jacket", 1999),
        ("Casual Shirt", "Regular-fit breathable casual shirt", 899),
        ("Travel Backpack", "Water-resistant everyday backpack", 1299),
        ("Sunglasses", "UV-protected lightweight sunglasses", 699),
        ("Analog Watch", "Minimal dial everyday wrist watch", 1499),
        ("Cushion Socks", "Cushioned cotton ankle socks", 299),
    ]),
    "Books & Stationery": (["Fieldnote", "Lumen", "Graphite", "Quill", "Scholar", "Papertrail"], [
        ("Dot-Grid Journal", "A5 hardbound dot-grid journal", 299),
        ("Reading Light", "USB rechargeable clip-on light", 449),
        ("Pencil Set", "Artist graphite pencil assortment", 249),
        ("Gel Pen Set", "Smooth-writing assorted gel pens", 199),
        ("Desk Organiser", "Multi-compartment desktop organiser", 399),
        ("Sketch Book", "Acid-free drawing and sketch book", 349),
        ("Exam Planner", "Undated study and revision planner", 279),
        ("Canvas Pouch", "Washable zip stationery pouch", 179),
    ]),
    "Beauty & Personal Care": (["Aura", "Bloom", "Dew", "Earthkind", "Nectar", "Pure"], [
        ("Face Wash", "Gentle daily cleansing face wash", 299),
        ("Moisturiser", "Lightweight everyday moisturiser", 399),
        ("Sunscreen SPF 50", "Broad-spectrum non-greasy sunscreen", 499),
        ("Shampoo", "Daily-care nourishing shampoo", 349),
        ("Body Lotion", "Fast-absorbing hydrating body lotion", 329),
        ("Hair Dryer", "Compact two-speed hair dryer", 899),
        ("Trimmer", "Rechargeable precision grooming trimmer", 1099),
        ("Bath Towel", "Soft high-absorbency cotton towel", 449),
    ]),
    "Sports & Fitness": (["Stride", "Summit", "Pulse", "Core", "Velocity", "Atlas"], [
        ("Yoga Mat", "Non-slip exercise and yoga mat", 699),
        ("Resistance Bands", "Five-level resistance band set", 499),
        ("Dumbbell Set", "Compact adjustable dumbbell pair", 2499),
        ("Cricket Bat", "Lightweight Kashmir willow cricket bat", 1699),
        ("Badminton Racquet", "Balanced graphite badminton racquet", 1199),
        ("Football", "Durable all-weather training football", 699),
        ("Gym Bag", "Ventilated sports and gym duffel", 899),
        ("Skipping Rope", "Adjustable high-speed skipping rope", 249),
    ]),
    "Grocery & Gourmet": (["Harvest", "Daily", "Nature", "Grainhouse", "SpiceRoute", "Goodness"], [
        ("Basmati Rice 5kg", "Long-grain aged basmati rice", 699),
        ("Cold-Pressed Oil 1L", "Cold-pressed everyday cooking oil", 399),
        ("Mixed Nuts 500g", "Roasted premium mixed nuts", 599),
        ("Green Tea 100 Bags", "Refreshing whole-leaf green tea", 449),
        ("Filter Coffee 500g", "Freshly roasted filter coffee blend", 399),
        ("Peanut Butter 1kg", "Creamy high-protein peanut butter", 499),
        ("Spice Box", "Six essential Indian ground spices", 349),
        ("Dark Chocolate", "Rich 70 percent cocoa chocolate", 199),
    ]),
    "Toys & Games": (["Wonder", "Bright", "Playcraft", "Quest", "Tiny", "Spark"], [
        ("Building Blocks", "Creative interlocking building block set", 799),
        ("Strategy Board Game", "Family strategy game for four players", 999),
        ("Remote Control Car", "Rechargeable remote-controlled racing car", 1299),
        ("Art Kit", "Complete drawing and colouring activity kit", 599),
        ("Jigsaw Puzzle", "Detailed 500-piece family puzzle", 449),
        ("Science Kit", "Hands-on beginner science experiment kit", 899),
        ("Plush Toy", "Soft washable animal plush toy", 499),
        ("Chess Set", "Folding magnetic chess set", 649),
    ]),
}


def products() -> list[tuple]:
    rows = []
    serial = 1
    # 8 categories x 6 brands x 8 products x 4 editions = 1,536 SKUs.
    for category, (brands, items) in CATEGORIES.items():
        for brand_index, brand in enumerate(brands):
            for item_index, (item, description, base_price) in enumerate(items):
                for edition in range(1, 5):
                    product_id = f"SKU-{serial:05d}"
                    price = base_price + edition * 50 + brand_index * 25
                    stock = (serial * 17) % 121
                    rating_value = 3.8 + ((serial * 7) % 12) / 10
                    reviews = 25 + (serial * 37) % 2400
                    rows.append((
                        product_id, f"{brand} {item} {edition}", category,
                        f"{description}; edition {edition}", price, stock,
                        f"{rating_value:.1f}★ ({reviews})", item.upper()[:24],
                        None, None, None, None, 1,
                    ))
                    serial += 1
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Replace the Cartisan catalog with generated test inventory")
    parser.add_argument("--confirm-replace", action="store_true", help="required because this deletes the existing catalog")
    args = parser.parse_args()
    if not args.confirm_replace:
        raise SystemExit("Refusing to replace catalog without --confirm-replace")

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
    store = Store(database_url=os.environ.get("SUPABASE_DATABASE_URL"))
    catalog = products()
    store.execute("DELETE FROM products")
    store.executemany(
        "INSERT INTO products (id,name,category,description,price,stock,rating,image_label,cross_sell_of,variant_of,options_json,option_values_json,active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        catalog,
    )
    count = store.rows("SELECT COUNT(*) AS count FROM products")[0]["count"]
    store.close()
    print(f"Seeded {count} products")


if __name__ == "__main__":
    main()
