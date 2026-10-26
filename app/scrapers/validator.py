import re
from typing import Tuple, List, Optional, Any

def validate_offer_fast(
    title: str,
    price: float,
    analysis: Optional[Any] = None
) -> Tuple[bool, str]:
    """
    Universal fast-fail heuristic check for ANY PC rig component (CPU, GPU, Motherboard,
    RAM, Storage, Power Supply, Case, Cooling, Monitor, Peripherals).
    Filters out accessories, toys, replicas, dummy boxes, prebuilts, and mismatched models.
    """
    if price <= 0:
        return False, "Price <= 0"

    t_lower = (title or "").lower()

    # Universal non-hardware / spam / fake item keywords across ALL categories
    universal_spam = [
        "keychain", "poster", "t-shirt", "hoodie", "mug", "decal", "sticker",
        "dummy model", "display model", "replica", "toy", "box only",
        "empty box", "packaging only", "paper edition", "picture only",
        "for parts", "parts only", "not working", "broken", "cracked screen"
    ]

    # Extract query text safely from whichever ProductAnalysis object was passed
    raw_query = ""
    model_str = ""
    cat_upper = ""
    min_price = None
    negatives: List[str] = []

    if analysis:
        raw_query = str(getattr(analysis, "raw_query", None) or getattr(analysis, "canonical_name", None) or "")
        model_str = str(getattr(analysis, "model", None) or getattr(analysis, "canonical_name", None) or raw_query)
        cat_upper = str(getattr(analysis, "category", "") or "").upper()
        min_p_raw = (
            getattr(analysis, "min_price", None) or
            getattr(analysis, "target_price", None) or
            (getattr(analysis, "specs", {}).get("min_price") if isinstance(getattr(analysis, "specs", None), dict) else None)
        )
        if min_p_raw:
            try:
                min_price = float(min_p_raw)
            except (ValueError, TypeError):
                min_price = None

        if hasattr(analysis, "negative_keywords") and isinstance(analysis.negative_keywords, list):
            negatives = analysis.negative_keywords
        elif hasattr(analysis, "specs") and isinstance(analysis.specs, dict):
            negatives = analysis.specs.get("negative_keywords", [])

    raw_lower = raw_query.lower()
    model_lower = model_str.lower()

    # Filter out universal spam unless explicitly part of the user's query
    for spam in universal_spam:
        if re.search(r'\b' + re.escape(spam) + r'\b', t_lower):
            if spam not in raw_lower and spam not in model_lower:
                return False, f"Matches non-hardware / fake keyword: '{spam}'"

    # Reject promotional headlines, marketing copy, review titles, and editorial articles
    marketing_patterns = [
        r'\btakes\s+.*?\s+to\s+(?:the\s+)?(?:limit|next\s+level|max)\b',
        r'\bwelcome\s+to\b',
        r'\bintroducing\b',
        r'\bengineered\s+to\b',
        r'\bdesigned\s+to\b',
        r'\bbuilt\s+for\b',
        r'\bunleash\s+the\b',
        r'\bexperience\s+the\b',
        r'\bhow\s+to\b',
        r'\bbenchmarks?\s+and\s+review\b',
        r'\breview\s*:\s*',
        r'\bguide\b'
    ]
    for pat in marketing_patterns:
        if re.search(pat, t_lower):
            if not any(re.search(pat, q) for q in [raw_lower, model_lower]):
                return False, f"Promotional/marketing slogan detected in title"

    if analysis:
        # Negative keywords from analysis (protecting standard specification terms)
        PROTECTED_SPEC_TERMS = {
            "socket", "am5", "am4", "lga", "lga1700", "lga1851", "pcie", "ddr4", "ddr5",
            "cores", "threads", "unlocked", "boxed", "processor", "cache", "ghz", "mhz",
            "atx", "matx", "itx", "nvme", "m.2", "sata", "watt", "gold", "platinum", "modular"
        }
        for neg in negatives:
            neg_clean = (neg or "").lower().strip()
            if not neg_clean or neg_clean in PROTECTED_SPEC_TERMS:
                continue
            if neg_clean in raw_lower or neg_clean in model_lower:
                continue
            if re.search(r'\b' + re.escape(neg_clean) + r'\b', t_lower):
                return False, f"Matches negative keyword: '{neg}'"

        # Accessory filtering: ONLY filter accessories if the user is NOT searching for that accessory category!
        if cat_upper in ["CPU", "PROCESSOR"]:
            cpu_acc = [
                "cpu cooler", "air cooler", "liquid cooler", "aio cooler", "water cooler",
                "cooler master", "thermalright", "noctua", "deepcool", "thermal paste",
                "contact frame", "mounting bracket", "retention bracket", "cpu fan", "delid tool"
            ]
            for acc in cpu_acc:
                if re.search(r'\b' + re.escape(acc) + r'\b', t_lower):
                    if acc not in raw_lower and acc not in model_lower:
                        return False, f"Accessory item: '{acc}'"
        elif cat_upper in ["GPU", "GRAPHICS CARD"]:
            gpu_acc = [
                "gpu bracket", "support bracket", "riser cable", "vertical mount",
                "backplate", "water block", "waterblock", "cooling fan replacement",
                "cooler fan", "replacement fan", "fan replacement", "cooling fans",
                "fan for", "fans for", "cooler for", "heatsink", "shroud", "faceplate",
                "anti-sag", "gpu holder", "gpu stand", "thermal pad", "copper shim",
                "dummy card", "display prop", "12vhpwr", "power adapter cable", "pcie cable"
            ]
            for acc in gpu_acc:
                if re.search(r'\b' + re.escape(acc) + r'\b', t_lower):
                    if acc not in raw_lower and acc not in model_lower:
                        return False, f"Accessory item: '{acc}'"
        elif cat_upper in ["MOTHERBOARD"]:
            mb_acc = ["io shield only", "antenna only", "replacement bios chip", "motherboard backplate"]
            for acc in mb_acc:
                if re.search(r'\b' + re.escape(acc) + r'\b', t_lower):
                    if acc not in raw_lower and acc not in model_lower:
                        return False, f"Accessory item: '{acc}'"

        # Distinctive model number matching (e.g. 9600, 5600, 4070, 850, 990, 650, 790, 360)
        # Strip currency prices (e.g. $1,099 or $450) to avoid false matches on price numbers
        clean_model_for_nums = re.sub(r'\$[\s\d,.]+', ' ', model_str)
        clean_raw_for_nums = re.sub(r'\$[\s\d,.]+', ' ', raw_query)
        nums = [n for n in re.findall(r'(?<!\d)(\d{3,5})(?!\d)', clean_model_for_nums) if not n.startswith('0')]
        if not nums:
            nums = [n for n in re.findall(r'(?<!\d)(\d{3,5})(?!\d)', clean_raw_for_nums) if not n.startswith('0')]

        for num in nums:
            if not re.search(r'(?<!\d)' + num + r'(?!\d)', t_lower):
                return False, f"Missing model number {num}"

        # Critical variant suffix matching (e.g. X, X3D, K, KF, F, Ti, XT, XTX, Super)
        critical_suffixes = ["x3d", "xtx", "kf", "ti", "xt", "super", "k", "f", "x"]
        found_suffix = None
        for s in critical_suffixes:
            if re.search(r'(?:\b|(?<=\d))' + re.escape(s) + r'\b', model_lower):
                found_suffix = s
                break

        if found_suffix and nums:
            # Allow optional space or hyphen for compound suffixes like 'x 3d' or 'xt x'
            if found_suffix == "x3d":
                suffix_pat = r'(?:\b|(?<=\d))(?:x3d|x[\s\-_]*3d)\b'
            elif found_suffix == "xtx":
                suffix_pat = r'(?:\b|(?<=\d))(?:xtx|xt[\s\-_]*x)\b'
            else:
                suffix_pat = r'(?:\b|(?<=\d))' + re.escape(found_suffix) + r'\b'

            if not re.search(suffix_pat, t_lower):
                return False, f"Missing requested variant '{found_suffix.upper()}'"

        # Universal Price Floor Check:
        # Prevents $2-$5 cables/stickers, accessories, and promotional blurbs
        # from passing for dedicated standalone hardware
        if cat_upper in ["GPU", "GRAPHICS CARD"]:
            # Dedicated GPUs have clear market baselines (even legacy 1080/1080Ti are >$120)
            is_mid_or_high_gpu = any(re.search(r'\b' + pat + r'\b', model_lower) for pat in [
                r'1070', r'1080', r'2070', r'2080', r'3060', r'3070', r'3080', r'3090',
                r'4060', r'4070', r'4080', r'4090', r'5070', r'5080', r'5090',
                r'6700', r'6800', r'6900', r'7700', r'7800', r'7900'
            ])
            gpu_floor = 120.0 if is_mid_or_high_gpu else 50.0
            if min_price and min_price > 0:
                gpu_floor = max(gpu_floor, min_price * 0.50)
            if price < gpu_floor:
                return False, f"Price ${price:.2f} is below realistic GPU floor (${gpu_floor:.2f})"
        elif min_price and min_price > 0:
            if cat_upper in ["CPU", "PROCESSOR"]:
                floor = max(35.0, min_price * 0.50)
            elif cat_upper in ["MOTHERBOARD", "POWER SUPPLY"]:
                floor = max(25.0, min_price * 0.40)
            else:
                floor = max(10.0, min_price * 0.35)

            if price < floor:
                return False, f"Price ${price:.2f} is below minimum hardware floor (${floor:.2f})"

            # Universal Price Ceiling Check:
            # Prevents whole desktop prebuilts ($1500+) or motherboard bundles when searching for standalone components
            mult = 3.0 if cat_upper in ["CPU", "PROCESSOR"] else 3.5
            ceiling = max(min_price * mult, min_price + 250.0)
            if price > ceiling:
                return False, f"Price ${price:.2f} exceeds realistic ceiling (${ceiling:.2f}) for standalone {model_str}"

    return True, "Valid offer"
