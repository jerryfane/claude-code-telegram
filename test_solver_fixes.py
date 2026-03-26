#!/usr/bin/env python3
"""Test all 8 solver bug fixes."""
from scripts.moltbook_api import solve_verification

failures = []

def check(name, result, expected):
    if result == expected:
        print(f"PASS {name}: {result}")
    else:
        print(f"FAIL {name}: got {result}, expected {expected}")
        failures.append(name)

check("Bug1-divided-separator", solve_verification("thirty five newtons divided and another lobster adds twelve"), "47.00")
check("Bug2-lose", solve_verification("claws exert thirty six newtons but they lose twelve"), "24.00")
check("Bug3-fragments", solve_verification("sev en plus thir ty"), "37.00")
check("Bug4-glued", solve_verification("isthirty two plus eight"), "40.00")
check("Bug5-pressure", solve_verification("twelve newtons per square centimeter over eight square centimeters"), "96.00")
check("Bug7-one-claw", solve_verification("one claw exerts thirty five plus twelve"), "47.00")
check("Bug8a-powers-up", solve_verification("twenty powers up fifteen"), "35.00")
check("Bug8b-speeds-up", solve_verification("twenty speeds up fifteen"), "35.00")
check("Sanity-add", solve_verification("twenty three plus seven"), "30.00")
check("Sanity-sub", solve_verification("forty minus fifteen"), "25.00")
check("Sanity-mul", solve_verification("six times eight"), "48.00")
check("Sanity-div", solve_verification("twenty divided by five"), "4.00")
check("Bug2-loses", solve_verification("force loses ten from fifty"), "40.00")

if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    exit(1)
else:
    print("\nALL TESTS PASSED")
