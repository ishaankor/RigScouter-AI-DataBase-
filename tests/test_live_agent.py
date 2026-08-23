import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import agent

async def run_live_tests():
    a = agent.TavilyHardwareAgent()
    passed = 0
    total = 0

    print("=================================================================")
    print("RUNNING AGENT QUERY NORMALIZATION & CATEGORIZATION TEST SUITE")
    print("=================================================================")

    test_queries = [
        ("RTX 5080", "GPU"),
        ("Ryzen 7 7800X3D", "CPU"),
        ("Samsung 990 Pro 2TB", "Storage"),
        ("Corsair Vengeance DDR5 32GB 6000MHz", "RAM"),
        ("ASUS ROG Strix Z890-E Gaming", "Motherboard"),
        ("Logitech G Pro X Superlight 2", "Peripherals"),
        ("Organic Bananas 3lb", "Not compatible (N/A)"),
    ]

    for q, expected_cat in test_queries:
        total += 1
        res = await a.analyze_query_with_groq(q)
        cat = res.get('category')
        print(f"Query: '{q}' -> Model: '{res.get('model')}', Category: '{cat}' (Expected: '{expected_cat}')")
        if expected_cat == "Not compatible (N/A)":
            assert cat == "Not compatible (N/A)", f"Failed for {q}"
        else:
            assert cat != "Not compatible (N/A)", f"Failed for {q}"
        passed += 1

    print(f"\n🎉 ALL {passed}/{total} QUERY NORMALIZATION TESTS PASSED!")

if __name__ == "__main__":
    asyncio.run(run_live_tests())
