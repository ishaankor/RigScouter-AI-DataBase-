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

    if analysis:
        # Negative keywords from analysis
        for neg in negatives:
            if neg.lower() in t_lower and neg.lower() not in raw_lower:
                return False, f"Matches negative keyword: '{neg}'"

        # Accessory filtering: ONLY filter accessories if the user is NOT searching for that accessory category!
        if cat_upper in ["CPU", "PROCESSOR"]:
            cpu_acc = [
                "cpu cooler", "air cooler", "liquid cooler", "aio cooler", "water cooler",
                "cooler master", "thermalright", "noctua", "deepcool", "thermal paste",
                "contact frame", "mounting bracket", "retention bracket", "cpu fan"
            ]
            for acc in cpu_acc:
                if re.search(r'\b' + re.escape(acc) + r'\b', t_lower):
                    if acc not in raw_lower and acc not in model_lower:
                        return False, f"Accessory item: '{acc}'"
        elif cat_upper in ["GPU", "GRAPHICS CARD"]:
            gpu_acc = [
                "gpu bracket", "support bracket", "riser cable", "vertical mount",
                "backplate", "water block", "cooling fan replacement"
            ]
            for acc in gpu_acc:
                if re.search(r'\b' + re.escape(acc) + r'\b', t_lower):
                    if acc not in raw_lower and acc not in model_lower:
                        return False, f"Accessory item: '{acc}'"
        elif cat_upper in ["MOTHERBOARD"]:
            mb_acc = ["io shield only", "antenna only", "replacement bios chip"]
            for acc in mb_acc:
                if re.search(r'\b' + re.escape(acc) + r'\b', t_lower):
                    if acc not in raw_lower and acc not in model_lower:
                        return False, f"Accessory item: '{acc}'"

        # Distinctive model number matching (e.g. 9600, 5600, 4070, 850, 990, 650, 790, 360)
        # Extract 3-5 digit model numbers from model or raw query
        nums = re.findall(r'(?<!\d)(\d{3,5})(?!\d)', model_str)
        if not nums:
            nums = re.findall(r'(?<!\d)(\d{3,5})(?!\d)', raw_query)

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
            if not re.search(r'(?:\b|(?<=\d))' + re.escape(found_suffix) + r'\b', t_lower):
                return False, f"Missing requested variant '{found_suffix.upper()}'"

        # Universal Price Floor Check:
        # Prevents $2-$5 cables/stickers from passing for expensive parts,
        # but allows genuinely cheap items (used parts, budget fans, thermal paste).
        if min_price and min_price > 0:
            floor = max(8.0, min_price * 0.15)
            if price < floor:
                return False, f"Price ${price:.2f} is below minimum hardware floor (${floor:.2f})"

            # Universal Price Ceiling Check:
            # Prevents whole desktop prebuilts ($1500+) or motherboard bundles when searching for standalone components
            mult = 3.0 if cat_upper in ["CPU", "PROCESSOR"] else 3.5
            ceiling = max(min_price * mult, min_price + 250.0)
            if price > ceiling:
                return False, f"Price ${price:.2f} exceeds realistic ceiling (${ceiling:.2f}) for standalone {model_str}"

    return True, "Valid offer"
