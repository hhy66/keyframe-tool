"""Row grouping ported from Flickr justified-layout 4.1.0 (lib/row.js, lib/index.js).

Copyright 2019 SmugMug, Inc. MIT License, see static/vendor/justified-layout-LICENSE.txt.
Only the decision of which items share a row is ported; the collage computes exact,
uncropped row geometry itself so every photo keeps its aspect ratio.
"""
import math


class _Row:
    def __init__(self, width, spacing, target_height, tolerance):
        self.width = width
        self.spacing = spacing
        self.target_height = target_height
        self.min_ratio = width / target_height * (1 - tolerance)
        self.max_ratio = width / target_height * (1 + tolerance)
        self.items = []
        self.complete = False

    def add(self, index, ratios):
        """Row.addItem: returns False when the item is rejected and the row is closed."""
        new = self.items + [index]
        new_ratio = sum(ratios[i] for i in new)
        target_ratio = (self.width - (len(new) - 1) * self.spacing) / self.target_height
        if new_ratio < self.min_ratio:
            self.items = new
            return True
        if new_ratio > self.max_ratio:
            if not self.items:
                self.items, self.complete = new, True
                return True
            previous_ratio = sum(ratios[i] for i in self.items)
            previous_target = (self.width - (len(self.items) - 1) * self.spacing) / self.target_height
            if abs(new_ratio - target_ratio) > abs(previous_ratio - previous_target):
                self.complete = True
                return False
            self.items, self.complete = new, True
            return True
        self.items, self.complete = new, True
        return True


def rows(ratios, width, spacing, target_height, tolerance=0.25):
    """computeLayout: group item indexes into rows, keeping widows in a final row."""
    result, row = [], None
    for index in range(len(ratios)):
        row = row or _Row(width, spacing, target_height, tolerance)
        added = row.add(index, ratios)
        if row.complete:
            result.append(row.items)
            row = _Row(width, spacing, target_height, tolerance)
            if not added:
                row.add(index, ratios)
                if row.complete:
                    result.append(row.items)
                    row = None
    if row and row.items:
        result.append(row.items)
    return result


def row_heights(groups, ratios, width, spacing):
    return [(width - spacing * (len(g) - 1)) / sum(ratios[i] for i in g) for g in groups]


def best_rows(ratios, width, spacing, target_ratio):
    """Sweep justified-layout's target row height and keep the grouping whose exact,
    fully justified result is closest to the requested overall shape with even rows."""
    if not ratios:
        return []
    low = width / sum(ratios) * 0.5
    high = width / min(ratios) * 1.5
    candidates = {}
    for step in range(97):
        height = low * (high / low) ** (step / 96)
        groups = rows(ratios, width, spacing, height)
        candidates.setdefault(tuple(map(tuple, groups)), groups)

    def score(groups):
        heights = row_heights(groups, ratios, width, spacing)
        if min(heights) <= 0:
            return math.inf
        total = sum(heights) + spacing * (len(groups) - 1)
        return abs(math.log(width / total / target_ratio)) + 0.5 * math.log(max(heights) / min(heights))

    return list(min(candidates.values(), key=lambda g: (round(score(g), 9), [len(r) for r in g])))
