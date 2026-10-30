"""The demo dataset: a small Indian online store. DEMO DATA ONLY.

Everything here is fictional. People use the reserved example.com domain, buildings and flats are invented, and prices are in
INR (the API stores money in minor units, paise, so Rs 699 is 69900). HSN codes are the commonly used headings for these goods,
included to show the tax-classification feature; they and any rates MUST be confirmed with the client's tax advisor.
"""

from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------------- store settings for the demo
# Checkout needs a shipping charge and a tax rate, and payments need their currency enabled (config.py: no defaults, on purpose). The seed uses these only if the .env
# has none, and tells you to put them in .env so the RUNNING api uses the same numbers.
DEMO_FLAT_SHIPPING_PAISE = 4900            # Rs 49 flat shipping ...
DEMO_FREE_SHIPPING_THRESHOLD_PAISE = 99900  # ... free from Rs 999 of goods
DEMO_TAX_RATE_PERCENT = "18"               # interim FLAT rate: NOT a GST calculation (see docs/STATUS.md)

ENV_LINES = (
    "PAYMENT_CURRENCIES_ENABLED=inr\n"
    f"FLAT_SHIPPING_CENTS={DEMO_FLAT_SHIPPING_PAISE}\n"
    f"FREE_SHIPPING_THRESHOLD_CENTS={DEMO_FREE_SHIPPING_THRESHOLD_PAISE}\n"
    "FREE_SHIPPING_THRESHOLD_BASIS=subtotal\n"
    f"TAX_RATE_PERCENT={DEMO_TAX_RATE_PERCENT}"
)

# --------------------------------------------------------------------------------------------- categories: (name, description, hue)
CATEGORIES = [
    ("Electronics", "Phones, laptops, tablets, and electronic accessories.", 210),
    ("Books", "Fiction, non-fiction, technical, and educational books.", 28),
    ("Home & Kitchen", "Appliances, cookware, furniture, and kitchen essentials.", 150),
    ("Office Supplies", "Stationery, desk accessories, and office equipment.", 48),
    ("Sports & Fitness", "Fitness equipment, sports accessories, and outdoor gear.", 0),
    ("Clothing", "Everyday clothing, casual wear, and accessories.", 280),
    ("Beauty & Personal Care", "Skincare, grooming products, and personal care essentials.", 330),
    ("Toys & Games", "Toys, board games, puzzles, and entertainment products.", 180),
]

# --------------------------------------------------------------------------------------------- tax classifications (HSN headings)
# code -> description. Created as DRAFT, approved by the demo admin, then assigned to products. No tax RULES, GST registration
# or business tax settings are seeded: they carry rates and the client's legal identity, which only the client can supply.
HSN = {
    "4901": "Printed books, brochures and similar printed matter",
    "4820": "Registers, notebooks, memo pads and similar stationery",
    "9608": "Ball point pens, felt-tipped and other pens",
    "8471": "Computers and peripherals (input/output units)",
    "8518": "Microphones, loudspeakers, headphones and earphones",
    "8507": "Electric accumulators (including power banks)",
    "8544": "Insulated wire, cable and other electric conductors",
    "8525": "Cameras (including webcams)",
    "8516": "Electric heating apparatus for household use",
    "7615": "Table, kitchen or other household articles of aluminium",
    "6911": "Tableware and kitchenware of porcelain or china",
    "3923": "Plastic containers and closures",
    "8211": "Knives with cutting blades, and sets",
    "9506": "Gymnastics, fitness and sports equipment",
    "6109": "T-shirts, singlets and vests, knitted",
    "6203": "Men's or boys' suits, trousers and jeans",
    "6110": "Jerseys, pullovers, sweatshirts and hoodies, knitted",
    "6105": "Men's or boys' shirts, knitted",
    "4202": "Trunks, bags and backpacks",
    "3304": "Beauty, skin-care and lip preparations",
    "6302": "Bed linen, table linen, toilet linen and kitchen linen",
    "9603": "Brushes",
    "9503": "Toys, puzzles and construction sets",
    "9504": "Playing cards and games",
}


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    rupees: int          # price in whole rupees; the API takes paise
    stock: int
    category: str
    hsn: str | None
    description: str
    status: str = "active"   # active | draft | archived

    @property
    def paise(self) -> int:
        return self.rupees * 100


PRODUCTS = [
    # Electronics
    Product("ELEC-MOUSE-001", "Wireless Mouse", 699, 50, "Electronics", "8471", "2.4 GHz wireless optical mouse with a silent-click design and a 12-month battery."),
    Product("ELEC-KB-001", "Mechanical Keyboard", 3499, 20, "Electronics", "8471", "Tenkeyless mechanical keyboard with tactile switches, white backlight and a detachable USB-C cable."),
    Product("ELEC-HUB-001", "USB-C Hub 7-in-1", 1799, 35, "Electronics", "8471", "HDMI 4K, two USB-A, SD and microSD readers, USB-C power pass-through and gigabit Ethernet."),
    Product("ELEC-HEADPHONE-001", "Bluetooth Headphones", 2499, 25, "Electronics", "8518", "Over-ear Bluetooth 5.3 headphones with 40-hour battery life and a built-in microphone."),
    Product("ELEC-WEBCAM-001", "1080p Webcam", 2299, 18, "Electronics", "8525", "Full HD webcam with autofocus, dual noise-reducing microphones and a privacy shutter."),
    Product("ELEC-POWERBANK-001", "Power Bank 20000 mAh", 1899, 40, "Electronics", "8507", "20000 mAh power bank with 22.5 W fast charging and two output ports."),
    Product("ELEC-STAND-001", "Aluminium Laptop Stand", 1299, 30, "Electronics", "8471", "Foldable, height-adjustable aluminium stand for laptops up to 17 inches."),
    Product("ELEC-CABLE-001", "USB-C Charging Cable 1 m", 349, 100, "Electronics", "8544", "Braided USB-C to USB-C cable, 60 W charging and 480 Mbps data."),
    Product("ELEC-EARBUDS-001", "True Wireless Earbuds", 1499, 0, "Electronics", "8518", "In-ear true wireless earbuds with a charging case. Currently out of stock."),
    # Books
    Product("BOOK-CC-001", "Clean Code", 899, 100, "Books", "4901", "A handbook of agile software craftsmanship by Robert C. Martin."),
    Product("BOOK-DDIA-001", "Designing Data-Intensive Applications", 1099, 40, "Books", "4901", "The big ideas behind reliable, scalable and maintainable systems, by Martin Kleppmann."),
    Product("BOOK-PP-001", "The Pragmatic Programmer", 999, 60, "Books", "4901", "Your journey to mastery, by David Thomas and Andrew Hunt."),
    Product("BOOK-PCC-001", "Python Crash Course", 849, 75, "Books", "4901", "A hands-on, project-based introduction to programming, by Eric Matthes."),
    Product("BOOK-AH-001", "Atomic Habits", 499, 90, "Books", "4901", "An easy and proven way to build good habits and break bad ones, by James Clear."),
    Product("BOOK-DW-001", "Deep Work", 449, 65, "Books", "4901", "Rules for focused success in a distracted world, by Cal Newport."),
    Product("BOOK-REFACTOR-001", "Refactoring", 1199, 35, "Books", "4901", "Improving the design of existing code, by Martin Fowler."),
    # Home & Kitchen
    Product("HOME-KETTLE-001", "Stainless Steel Electric Kettle 1.5 L", 1299, 30, "Home & Kitchen", "8516", "1500 W electric kettle with auto shut-off and boil-dry protection."),
    Product("HOME-PAN-001", "Non-Stick Frying Pan 26 cm", 999, 45, "Home & Kitchen", "7615", "Induction-compatible non-stick frying pan with a heat-resistant handle."),
    Product("HOME-RICECOOKER-001", "Electric Rice Cooker 1.8 L", 2799, 22, "Home & Kitchen", "8516", "Cooks rice for up to six people and keeps it warm automatically."),
    Product("HOME-DINNERSET-001", "Ceramic Dinner Set, 18 Pieces", 3299, 15, "Home & Kitchen", "6911", "Microwave- and dishwasher-safe dinner set for six."),
    Product("HOME-CONTAINER-001", "Airtight Storage Containers, Set of 3", 799, 55, "Home & Kitchen", "3923", "BPA-free airtight containers in three sizes for the pantry and fridge."),
    Product("HOME-KNIFE-001", "Kitchen Knife Set, 6 Pieces", 1499, 25, "Home & Kitchen", "8211", "Stainless steel knives with ergonomic handles and a wooden block."),
    Product("HOME-PURIFIER-001", "Smart Water Purifier", 14999, 0, "Home & Kitchen", None, "Coming soon: RO + UV purifier with a filter-change indicator.", "draft"),
    # Office Supplies
    Product("OFFICE-NOTEBOOK-001", "A5 Hardcover Notebook", 249, 150, "Office Supplies", "4820", "200 ruled pages of 80 gsm paper in a hardcover binding."),
    Product("OFFICE-PENS-001", "Gel Pen Set, 10 Pack", 199, 200, "Office Supplies", "9608", "Smooth-writing 0.5 mm gel pens in ten colours."),
    Product("OFFICE-ORGANIZER-001", "Desk Organizer", 599, 70, "Office Supplies", None, "Wooden desk organizer with compartments for pens, phone and notes."),
    Product("OFFICE-MAT-001", "Ergonomic Desk Mat", 899, 35, "Office Supplies", None, "Large non-slip desk mat with a stitched edge, 80 x 40 cm."),
    Product("OFFICE-STICKY-001", "Sticky Notes Pack", 149, 180, "Office Supplies", "4820", "Twelve pads of 100 sheets in assorted colours."),
    Product("OFFICE-SPIRAL-001", "Spiral Notebook (Discontinued)", 99, 0, "Office Supplies", "4820", "Discontinued line, kept for order history.", "archived"),
    # Sports & Fitness
    Product("SPORT-YOGA-001", "Yoga Mat 6 mm", 899, 50, "Sports & Fitness", "9506", "Non-slip 6 mm yoga mat with a carry strap."),
    Product("SPORT-DUMBBELL-001", "Adjustable Dumbbell Set 20 kg", 3999, 3, "Sports & Fitness", "9506", "Adjustable dumbbell set with a storage tray. Only a few left."),
    Product("SPORT-BANDS-001", "Resistance Band Set of 5", 799, 65, "Sports & Fitness", "9506", "Five resistance levels with a carry bag and exercise guide."),
    Product("SPORT-BOTTLE-001", "Insulated Water Bottle 750 ml", 799, 80, "Sports & Fitness", None, "Double-wall stainless steel bottle: cold for 24 hours, hot for 12."),
    Product("SPORT-JUMPROPE-001", "Speed Jump Rope", 299, 75, "Sports & Fitness", "9506", "Adjustable speed rope with ball bearings and foam grips."),
    # Clothing
    Product("CLOTH-TSHIRT-001", "Classic Cotton T-Shirt", 499, 100, "Clothing", "6109", "100% combed cotton crew-neck t-shirt, regular fit."),
    Product("CLOTH-JEANS-001", "Slim Fit Jeans", 1499, 45, "Clothing", "6203", "Stretch denim jeans in a slim fit, mid rise."),
    Product("CLOTH-HOODIE-001", "Hooded Sweatshirt", 1299, 35, "Clothing", "6110", "Fleece-lined cotton blend hoodie with a kangaroo pocket."),
    Product("CLOTH-BACKPACK-001", "Canvas Backpack", 999, 50, "Clothing", "4202", "Water-resistant canvas backpack with a padded 15-inch laptop sleeve."),
    Product("CLOTH-POLO-001", "Casual Polo Shirt", 699, 60, "Clothing", "6105", "Breathable piqué cotton polo shirt."),
    # Beauty & Personal Care
    Product("BEAUTY-CLEANSER-001", "Daily Face Cleanser 100 ml", 349, 60, "Beauty & Personal Care", "3304", "Gentle gel cleanser for all skin types."),
    Product("BEAUTY-CREAM-001", "Moisturizing Hand Cream 50 g", 199, 90, "Beauty & Personal Care", "3304", "Fast-absorbing hand cream with shea butter."),
    Product("BEAUTY-BRUSH-001", "Bamboo Hair Brush", 299, 45, "Beauty & Personal Care", "9603", "Detangling hair brush with a bamboo handle and natural bristles."),
    Product("BEAUTY-LOTION-001", "Body Lotion 250 ml", 399, 70, "Beauty & Personal Care", "3304", "Light daily body lotion with aloe vera."),
    Product("BEAUTY-LIPBALM-001", "Lip Balm", 149, 120, "Beauty & Personal Care", "3304", "Nourishing lip balm with SPF 15."),
    # Toys & Games
    Product("TOY-PUZZLE-001", "Wooden Puzzle Set", 499, 40, "Toys & Games", "9503", "Set of four animal puzzles in painted wood, ages 3+."),
    Product("TOY-BOARDGAME-001", "Strategy Board Game", 1499, 25, "Toys & Games", "9504", "A two-to-four player strategy game, ages 10+."),
    Product("TOY-BLOCKS-001", "Building Blocks Set, 150 Pieces", 999, 55, "Toys & Games", "9503", "Colourful interlocking blocks with a storage box, ages 4+."),
    Product("TOY-JIGSAW-001", "Jigsaw Puzzle 1000 Pieces", 799, 35, "Toys & Games", "9503", "A 1000-piece jigsaw with a scenic landscape picture."),
    Product("TOY-CARDS-001", "Playing Cards", 149, 100, "Toys & Games", "9504", "Standard 52-card deck with a plastic coating."),
]


# The application will not let the administrator who created a tax record also approve it (four-eyes control), so the demo has a
# second administrator who approves the demo tax classifications. It also means the store never depends on a single admin.
APPROVER_USERNAME = "approver"
APPROVER_EMAIL = "approver@example.com"

# --------------------------------------------------------------------------------------------- products with options (variants)
@dataclass(frozen=True)
class VariantProduct:
    sku: str
    name: str
    rupees: int
    category: str
    hsn: str | None
    description: str
    options: dict[str, list[str]]
    # (variant sku, {option: value}, stock, price override in rupees or None). Only the combinations that are actually sold exist.
    variants: list[tuple[str, dict[str, str], int, int | None]] = field(default_factory=list)


VARIANT_PRODUCTS = [
    VariantProduct(
        "CLOTH-TEE-VAR", "Everyday Crew Tee", 599, "Clothing", "6109",
        "Soft combed-cotton crew-neck tee in four sizes and three colours. Not every combination is made.",
        {"Size": ["S", "M", "L", "XL"], "Colour": ["Black", "Navy", "White"]},
        [("TEE-S-BLK", {"Size": "S", "Colour": "Black"}, 20, None), ("TEE-M-BLK", {"Size": "M", "Colour": "Black"}, 25, None),
         ("TEE-L-BLK", {"Size": "L", "Colour": "Black"}, 18, None), ("TEE-XL-BLK", {"Size": "XL", "Colour": "Black"}, 8, 649),
         ("TEE-S-NVY", {"Size": "S", "Colour": "Navy"}, 12, None), ("TEE-M-NVY", {"Size": "M", "Colour": "Navy"}, 15, None),
         ("TEE-L-NVY", {"Size": "L", "Colour": "Navy"}, 0, None), ("TEE-M-WHT", {"Size": "M", "Colour": "White"}, 10, None)],
    ),
    VariantProduct(
        "HOME-SHEET-VAR", "Cotton Bedsheet Set", 1299, "Home & Kitchen", "6302",
        "Percale cotton bedsheet with two pillow covers, in the size of your bed.",
        {"Size": ["Single", "Double", "King"]},
        [("SHEET-SGL", {"Size": "Single"}, 14, 999), ("SHEET-DBL", {"Size": "Double"}, 22, None), ("SHEET-KNG", {"Size": "King"}, 9, 1699)],
    ),
]
VARIANT_STOCK = {sku: stock for vp in VARIANT_PRODUCTS for sku, _o, stock, _p in vp.variants}

# --------------------------------------------------------------------------------------------- customers
@dataclass(frozen=True)
class Customer:
    username: str
    email: str
    recipient: str
    line1: str
    line2: str
    city: str
    state: str
    postal_code: str


# All fictional. example.com is reserved for documentation: these addresses can never reach a real person.
CUSTOMERS = [
    Customer("priya_sharma", "priya.sharma@example.com", "Priya Sharma", "Flat 402, Lotus Residency", "Linking Road, Bandra West", "Mumbai", "Maharashtra", "400050"),
    Customer("rahul_verma", "rahul.verma@example.com", "Rahul Verma", "B-17, Green Park Apartments", "Connaught Place", "New Delhi", "Delhi", "110001"),
    Customer("ananya_iyer", "ananya.iyer@example.com", "Ananya Iyer", "No. 21, 12th Main", "Indiranagar", "Bengaluru", "Karnataka", "560038"),
    Customer("mohammed_faiz", "mohammed.faiz@example.com", "Mohammed Faiz", "Plot 56, Road No. 3", "Banjara Hills", "Hyderabad", "Telangana", "500034"),
    Customer("sneha_kulkarni", "sneha.kulkarni@example.com", "Sneha Kulkarni", "Sai Heights, Wing B, 7th Floor", "Koregaon Park", "Pune", "Maharashtra", "411001"),
    Customer("arjun_mehta", "arjun.mehta@example.com", "Arjun Mehta", "12, Shreeji Society", "Navrangpura", "Ahmedabad", "Gujarat", "380009"),
]

GUEST = {
    "email": "vikram.nair@example.com",
    "address": {
        "recipient_name": "Vikram Nair", "line1": "TC 25/1234, Palm Grove", "line2": "Kowdiar",
        "city": "Thiruvananthapuram", "state": "Kerala", "postal_code": "695003", "country": "IN",
    },
}


# --------------------------------------------------------------------------------------------- orders
@dataclass(frozen=True)
class OrderPlan:
    key: str                       # idempotency key (demo-seed-NN): also how the seed recognises its own orders
    customer: int | None           # index into CUSTOMERS; None = guest
    items: list[tuple[str, int]]   # (sku, quantity)
    outcome: str                   # fulfilled | paid | refunded | cancel_and_refund | customer_cancelled | guest_paid
    days_ago: int
    extra: dict = field(default_factory=dict)


ORDERS = [
    OrderPlan("demo-seed-01", 0, [("ELEC-HEADPHONE-001", 1), ("ELEC-CABLE-001", 2)], "fulfilled", 28),
    OrderPlan("demo-seed-02", 1, [("BOOK-CC-001", 1), ("BOOK-PP-001", 1), ("BOOK-AH-001", 1)], "fulfilled", 26),
    OrderPlan("demo-seed-03", 2, [("HOME-KETTLE-001", 1), ("HOME-KNIFE-001", 1)], "fulfilled", 24),
    OrderPlan("demo-seed-04", 3, [("ELEC-KB-001", 1), ("ELEC-MOUSE-001", 1)], "fulfilled", 21),
    OrderPlan("demo-seed-05", 4, [("SPORT-YOGA-001", 1), ("SPORT-BANDS-001", 1), ("SPORT-BOTTLE-001", 1)], "fulfilled", 19),
    OrderPlan("demo-seed-06", 5, [("CLOTH-TSHIRT-001", 3)], "fulfilled", 17),
    OrderPlan("demo-seed-07", 0, [("ELEC-POWERBANK-001", 1)], "fulfilled", 15),
    OrderPlan("demo-seed-08", 1, [("CLOTH-JEANS-001", 1), ("CLOTH-POLO-001", 2)], "refunded", 13),
    OrderPlan("demo-seed-09", 2, [("BOOK-DW-001", 1), ("OFFICE-NOTEBOOK-001", 2), ("OFFICE-PENS-001", 2)], "fulfilled", 11),
    OrderPlan("demo-seed-17", 2, [("TEE-M-BLK", 2), ("TEE-L-BLK", 1), ("TEE-S-NVY", 1)], "fulfilled", 10),
    OrderPlan("demo-seed-10", 3, [("TOY-BOARDGAME-001", 1), ("TOY-CARDS-001", 2)], "fulfilled", 9),
    OrderPlan("demo-seed-11", 4, [("ELEC-HUB-001", 1), ("ELEC-STAND-001", 1)], "paid", 7),
    OrderPlan("demo-seed-12", 5, [("HOME-RICECOOKER-001", 1)], "paid", 5),
    OrderPlan("demo-seed-18", 1, [("SHEET-KNG", 1), ("TEE-XL-BLK", 1)], "paid", 5),
    OrderPlan("demo-seed-13", 0, [("BEAUTY-CLEANSER-001", 1), ("BEAUTY-CREAM-001", 1), ("BEAUTY-LIPBALM-001", 2)], "paid", 4),
    OrderPlan("demo-seed-14", 1, [("ELEC-WEBCAM-001", 1)], "cancel_and_refund", 3),
    OrderPlan("demo-seed-15", 2, [("CLOTH-BACKPACK-001", 1)], "customer_cancelled", 2),
    OrderPlan("demo-seed-16", None, [("SPORT-DUMBBELL-001", 1), ("SPORT-BANDS-001", 1)], "guest_paid", 1),
]

# Couriers used for the demo shipments (names only: NovaCart calls no courier). Tracking numbers and links are obviously fake.
DEMO_CARRIERS = ["Delhivery", "Blue Dart", "DTDC", "India Post (Speed Post)", "Ecom Express", "Xpressbees"]
DEMO_TRACKING_URL = "https://tracking.example.com/track?awb={number}"
DEMO_DELIVERED_AFTER_DAYS = 15   # orders older than this show as delivered

# Reviews by customers who bought the product in a paid or shipped order above: (customer index, sku, rating, title, body).
DEMO_REVIEWS = [
    (0, "ELEC-HEADPHONE-001", 5, "Great sound for the price", "Comfortable for long calls and the battery easily lasts the week."),
    (0, "ELEC-CABLE-001", 4, "Sturdy cable", "Charges fast. A little short for my desk setup."),
    (2, "TEE-M-BLK", 5, "Perfect fit", "True to size and the cotton is soft. Ordering another colour."),
    (2, "HOME-KETTLE-001", 4, "Boils quickly", "Does the job. The lid could be a bit tighter."),
    (3, "ELEC-KB-001", 5, "Lovely typing feel", "Solid build and the switches are satisfying."),
    (3, "ELEC-MOUSE-001", 3, "Okay mouse", "Works well but the scroll wheel is loud."),
    (1, "BOOK-CC-001", 5, "Worth reading", "Practical advice I use every week."),
    (1, "BOOK-AH-001", 4, "Easy and useful", "Short chapters, simple ideas that actually stick."),
    (4, "SPORT-YOGA-001", 4, "Good grip", "Does not slip, and easy to roll up."),
    (4, "SPORT-BOTTLE-001", 5, "Keeps drinks cold", "Still cold after a full day out."),
    (5, "CLOTH-TSHIRT-001", 2, "Shrank after washing", "Comfortable, but it came out a size smaller."),
    (0, "ELEC-POWERBANK-001", 5, "Charges my phone four times", None),
]

# Products saved for later: (customer index, [skus]).
DEMO_WISHLISTS = [
    (0, ["ELEC-WEBCAM-001", "CLOTH-TEE-VAR", "SPORT-DUMBBELL-001"]),
    (1, ["HOME-RICECOOKER-001", "TOY-BOARDGAME-001"]),
    (3, ["CLOTH-JEANS-001", "HOME-SHEET-VAR"]),
]

# Left in the customers' carts (not checked out), so a customer login shows a cart.
CARTS = [
    (3, [("HOME-DINNERSET-001", 1), ("HOME-CONTAINER-001", 2)]),
    (4, [("TOY-JIGSAW-001", 1)]),
    (5, [("ELEC-STAND-001", 1), ("OFFICE-MAT-001", 1)]),
    (1, [("TEE-M-NVY", 2)]),
]
