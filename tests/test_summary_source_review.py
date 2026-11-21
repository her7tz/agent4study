"""Source-review data flow and execution boundaries, without model accuracy claims."""
import copy
import json
import unittest

from runtime_control import CancellationToken, RunCancelled
from summary_engine import SourceBlock, SummaryEngine, SummaryError


class ReviewModel:
    """A scripted seam: review can correct the deliberately lossy merge output."""

    def __init__(self, review=None, separate=False, content="The condition was omitted."):
        self.inputs = []
        self.cancellations = []
        self.review = review
        self.separate = separate
        self.content = content

    def json_chat(self, system, payload, **kwargs):
        data = json.loads(payload)
        self.inputs.append(data)
        self.cancellations.append(kwargs.get("cancellation"))
        if data.get("source_review"):
            result = copy.deepcopy({"overview": data["overview"], "units": data["units"]})
            return self.review(data, result) if self.review else result
        if "blocks" in data:
            source_ids = list(dict.fromkeys(block["id"] for block in data["blocks"]))
            groups = [[source_id] for source_id in source_ids] if self.separate else [source_ids]
            return {"overview": "An unchecked overview.", "units": [
                {"title": "Topic " + str(index), "content": self.content, "source_ids": sources}
                for index, sources in enumerate(groups, 1)]}
        notes = data["notes"]
        groups = [[note] for note in notes] if self.separate else [notes]
        return {"overview": "An unchecked overview.", "units": [
            {"title": "Topic " + str(index), "content": self.content,
             "source_ids": list(dict.fromkeys(source for note in group for source in note["source_ids"])),
             "merged_from": [note["id"] for note in group]}
            for index, group in enumerate(groups, 1)]}

    def reviews(self):
        return [data for data in self.inputs if data.get("source_review")]


class SourceReviewTests(unittest.TestCase):
    def test_paper_window_review_reads_cross_page_continuation_within_budget(self):
        blocks = [SourceBlock('p1', 'Background. ' * 70),
                  SourceBlock('p2', 'Sampling details. ' * 45 + 'We sample about 10'),
                  SourceBlock('p3', 'successful trajectories per agent. ' + 'Method. ' * 95),
                  SourceBlock('p4', 'Results. ' * 95),
                  SourceBlock('p5', 'Limitations. ' * 70)]

        def review(data, result):
            visible = {block['id']: block['text'] for block in data['blocks']}
            if 'p2' in visible and 'p3' in visible:
                for unit in result['units']:
                    if 'p2' in unit['source_ids']:
                        unit['content'] = 'About 10 successful trajectories per agent.'
                        if 'p3' not in unit['source_ids']:
                            unit['additional_support'] = [{'source_id': 'p3',
                                'quote': 'successful trajectories per agent.'}]
            return result

        model = ReviewModel(review=review, separate=True)
        engine = SummaryEngine(model)
        engine.CHUNK_CHARS = 1200
        engine.REVIEW_CHARS = 3300
        result = engine.summarize(blocks, kind='paper', title='Sampling', review_full_material=True)
        sampling = next(unit for unit in result.units if 'p2' in unit['source_ids'])
        self.assertEqual(sampling['content'], 'About 10 successful trajectories per agent.')
        self.assertIn('p3', sampling['source_ids'])
        self.assertEqual(result.processing['source_review_scope'], 'source_windows')
        self.assertTrue(any(block.get('boundary_context') for data in model.reviews() for block in data['blocks']))
        self.assertTrue(all(len(json.dumps(data, ensure_ascii=False, separators=(',', ':'))) <= engine.REVIEW_CHARS
                            for data in model.reviews()))

    def summarize(self, model, blocks, **kwargs):
        return SummaryEngine(model, **kwargs).summarize(
            blocks, kind="fixture", title="Source review", scope="Extracted text only; no image analysis.").as_dict()

    def test_original_text_and_reader_scope_reach_review_and_revisions_reach_bundle(self):
        blocks = [SourceBlock("p3", "The method terminates only on finite graphs.", {"page": 3}),
                  SourceBlock("p9", "A visited set avoids repeated processing.", {"page": 9})]

        def correct(data, result):
            self.assertEqual([block["text"] for block in data["blocks"]], [block.text for block in blocks])
            self.assertEqual(data["blocks"][1]["locator"], {"page": 9})
            self.assertEqual(data["scope"], "Extracted text only; no image analysis.")
            self.assertTrue(data["source_subset"])
            result["overview"] = "Termination requires a finite graph."
            result["units"][0]["content"] = "On finite graphs, a visited set avoids repeated processing."
            # Model-supplied location/scope fields never replace reader metadata.
            result["units"][0]["locator"] = {"page": 999}
            result["scope"] = "Images were inspected."
            return result

        model = ReviewModel(review=correct)
        bundle = self.summarize(model, blocks)
        self.assertEqual(bundle["overview"], "Termination requires a finite graph.")
        self.assertEqual(bundle["units"][0]["content"],
                         "On finite graphs, a visited set avoids repeated processing.")
        self.assertEqual(bundle["units"][0]["id"], "k1")
        self.assertEqual(bundle["sources"]["p9"]["page"], 9)
        self.assertNotIn("locator", bundle["units"][0])
        self.assertEqual(bundle["processing"]["source_review_batches"], 1)
        self.assertEqual(bundle["processing"]["reviewed_units"], 1)
        self.assertEqual(bundle["processing"]["model_calls"], len(model.inputs))

    def test_review_response_order_cannot_reorder_stable_knowledge_ids(self):
        def reorder(data, result):
            result["units"].reverse()
            return result

        model = ReviewModel(review=reorder, separate=True)
        bundle = self.summarize(model, [SourceBlock("first", "First topic."),
                                        SourceBlock("second", "Second topic.")])
        self.assertEqual([unit["id"] for unit in bundle["units"]], ["k1", "k2"])
        self.assertEqual([unit["source_ids"] for unit in bundle["units"]], [["first"], ["second"]])

    def test_review_cannot_drop_duplicate_replace_or_uncite_knowledge(self):
        def missing(result):
            result["units"] = []

        def duplicate(result):
            result["units"].append(copy.deepcopy(result["units"][0]))

        def unknown(result):
            result["units"][0]["id"] = "k99"

        def lose_source(result):
            result["units"][0]["source_ids"] = ["p3"]

        def invent_source(result):
            result["units"][0]["source_ids"].append("unread")

        def omit_overview(result):
            result.pop("overview")

        def unsupported(result):
            result["unsupported_unit_ids"] = ["k1"]

        for mutation in (missing, duplicate, unknown, lose_source, invent_source, omit_overview, unsupported):
            with self.subTest(mutation=mutation.__name__):
                def alter(data, result):
                    mutation(result)
                    return result

                model = ReviewModel(review=alter)
                with self.assertRaises(SummaryError):
                    self.summarize(model, [SourceBlock("p3", "Method."), SourceBlock("p9", "Evidence.")])
                self.assertEqual(len(model.reviews()), 1)

    def test_long_single_source_is_reviewed_in_full_original_windows_with_accumulated_revisions(self):
        text = "Long source material. " * 1700 + "THE FINAL CORRECTION"

        def revise_each_window(data, result):
            suffix = " Window {} checked.".format(data["chunk_index"])
            result["units"][0]["content"] += suffix
            result["overview"] += suffix
            return result

        model = ReviewModel(review=revise_each_window)
        bundle = self.summarize(model, [SourceBlock("long", text, {"message_id": 7, "role": "user"})])
        extraction = [data for data in model.inputs if "blocks" in data and not data.get("source_review")]
        reviews = model.reviews()
        self.assertGreater(len(reviews), 1)
        self.assertEqual([data["blocks"] for data in reviews], [data["blocks"] for data in extraction])
        self.assertEqual("".join(block["text"] for data in reviews for block in data["blocks"]), text)
        self.assertTrue(all(len(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
                            <= SummaryEngine.REVIEW_CHARS for data in reviews))
        self.assertEqual(bundle["units"][0]["source_ids"], ["long"])
        self.assertEqual(bundle["sources"]["long"]["role"], "user")
        for index in range(1, len(reviews) + 1):
            self.assertIn("Window {} checked.".format(index), bundle["units"][0]["content"])
            self.assertIn("Window {} checked.".format(index), bundle["overview"])
        self.assertEqual(bundle["processing"]["source_review_batches"], len(extraction))

    def test_original_window_ranges_prevent_reviewing_every_part_of_a_long_source_for_each_unit(self):
        model = ReviewModel(separate=True)
        bundle = self.summarize(model, [SourceBlock("long", "Bounded message. " * 2200)])
        reviews = model.reviews()
        self.assertEqual(len(reviews), bundle["processing"]["chunks"])
        self.assertTrue(all(len(data["units"]) == 1 for data in reviews))
        reviewed_ids = [data["units"][0]["id"] for data in reviews]
        self.assertEqual(reviewed_ids, [unit["id"] for unit in bundle["units"]])

    def test_opt_in_full_review_can_repair_a_later_source_missing_from_the_unit(self):
        quote = "The learned method obtained 92.5 points."
        method = SourceBlock("method", "The method encodes a position. " * 20, {"page": 6})
        experiment = SourceBlock("result", quote + " The score is on the development set." * 20, {"page": 9})

        def correct(data, result):
            self.assertFalse(data["source_subset"])
            self.assertEqual([block["text"] for block in data["blocks"]], [method.text, experiment.text])
            unit = next(item for item in result["units"] if item["source_ids"] == ["method"])
            unit["content"] = "The position encoder reached 92.5 on the development set."
            unit["additional_support"] = [{"source_id": "result", "quote": quote}]
            result["overview"] = "Method and experiment text were both provided."
            return result

        model = ReviewModel(review=correct, separate=True)
        engine = SummaryEngine(model)
        engine.CHUNK_CHARS = 1200
        bundle = engine.summarize([method, experiment], kind="paper", title="Method and result",
                                  review_full_material=True).as_dict()
        self.assertEqual(bundle["processing"]["chunks"], 2)
        self.assertEqual(bundle["processing"]["source_review_scope"], "full_material")
        self.assertEqual(len(model.reviews()), 1)
        self.assertEqual(bundle["overview"], "Method and experiment text were both provided.")
        self.assertEqual(bundle["units"][0]["source_ids"], ["method", "result"])
        self.assertEqual(bundle["units"][0]["additional_support"], [{"source_id": "result", "quote": quote}])
        self.assertEqual(bundle["sources"]["result"]["page"], 9)

    def test_full_review_batches_share_one_bounded_original_input_and_overview(self):
        def revise_overview(data, result):
            result["overview"] = "First global review. " + "x" * 1600 if data["units"][0]["id"] == "k1" else "Second view."
            return result

        model = ReviewModel(review=revise_overview, separate=True, content="Supporting explanation. " * 35)
        engine = SummaryEngine(model)
        engine.REVIEW_CHARS = 1600
        bundle = engine.summarize([SourceBlock("a", "The first original passage."),
                                   SourceBlock("b", "The second original passage.")],
                                  kind="paper", title="Complete source review", review_full_material=True)
        self.assertEqual(bundle.processing["source_review_scope"], "full_material")
        self.assertEqual(len(model.reviews()), 2)
        self.assertTrue(bundle.overview.startswith("First global review."))
        self.assertEqual(model.reviews()[0]["overview"], model.reviews()[1]["overview"])
        self.assertTrue(all(not data["source_subset"] and len(data["blocks"]) == 2
                            for data in model.reviews()))
        self.assertTrue(all(len(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
                            <= engine.REVIEW_CHARS for data in model.reviews()))

    def test_opt_in_window_fallback_accepts_only_evidence_visible_in_that_window(self):
        blocks = [SourceBlock("method", "Method text. " * 24),
                  SourceBlock("near", "Adjacent experiment. " * 20),
                  SourceBlock("far", "A different experiment. " * 42)]

        class OverlappingUnitsModel(ReviewModel):
            def json_chat(self, system, payload, **kwargs):
                result = super().json_chat(system, payload, **kwargs)
                if "notes" in json.loads(payload):
                    first, near, far = result["units"]
                    first["source_ids"] += far["source_ids"]
                    first["merged_from"] += far["merged_from"]
                    result["units"] = [first, near]
                return result

        def run(addition):
            def revise(data, result):
                self.assertTrue(data["source_subset"])
                if data["chunk_index"] == 1:
                    self.assertEqual([block["id"] for block in data["blocks"]], ["method", "near"])
                    result["units"][0]["additional_support"] = [addition]
                else:
                    self.assertEqual(data["units"][0]["source_ids"], ["method", "far", "near"])
                    self.assertNotIn("additional_support", data["units"][0])
                return result

            model = OverlappingUnitsModel(review=revise, separate=True)
            engine = SummaryEngine(model)
            engine.CHUNK_CHARS = 1150
            engine.REVIEW_CHARS = 1700
            return engine.summarize(blocks, kind="paper", title="Oversized complete source",
                                    review_full_material=True)

        valid = {"source_id": "near", "quote": "Adjacent experiment."}
        bundle = run(valid)
        self.assertEqual(bundle.processing["source_review_scope"], "source_windows")
        self.assertEqual(bundle.units[0]["source_ids"], ["method", "far", "near"])
        self.assertEqual(bundle.units[0]["additional_support"], [valid])
        with self.assertRaisesRegex(SummaryError, "补充依据"):
            run({"source_id": "far", "quote": "A different experiment."})

    def test_opt_in_review_rejects_unproved_or_duplicate_additions(self):
        blocks = [SourceBlock("a", "Method evidence here."), SourceBlock("b", "An exact result quote.")]
        correct = {"source_id": "b", "quote": "An exact result quote."}
        for additions in (
            [{"source_id": "b", "quote": "Method evidence here."}],
            [{"source_id": "unknown", "quote": "An exact result quote."}],
            [{"source_id": "a", "quote": "Method evidence here."}],
            [{"source_id": "b", "quote": " "}],
            [correct, correct],
        ):
            with self.subTest(additions=additions):
                def revise(data, result):
                    result["units"][0]["additional_support"] = additions
                    return result

                model = ReviewModel(review=revise, separate=True)
                with self.assertRaisesRegex(SummaryError, "补充依据"):
                    SummaryEngine(model).summarize(blocks, kind="paper", title="Invalid reference",
                                                   review_full_material=True)
                self.assertEqual(len(model.reviews()), 2)

    def test_review_can_correct_an_inexact_supplemental_quote_once(self):
        blocks = [SourceBlock("a", "Method evidence here."), SourceBlock("b", "An exact result  quote.")]
        statuses = []

        def revise(data, result):
            quote = "An exact result  quote." if len(model.reviews()) == 2 else "An exact result quote."
            result["units"][0]["additional_support"] = [{"source_id": "b", "quote": quote}]
            return result

        model = ReviewModel(review=revise, separate=True)
        bundle = SummaryEngine(model, progress=statuses.append).summarize(
            blocks, kind="paper", title="Corrected quote", review_full_material=True)
        self.assertEqual(len(model.reviews()), 2)
        self.assertEqual(model.reviews()[0], model.reviews()[1])
        self.assertEqual(bundle.units[0]["source_ids"], ["a", "b"])
        self.assertEqual(bundle.units[0]["additional_support"],
                         [{"source_id": "b", "quote": "An exact result  quote."}])
        self.assertTrue(any("整合已取得材料" in status for status in statuses))
        self.assertTrue(any("对照本次读取的材料核对知识" in status for status in statuses))
        self.assertTrue(any("重新核对补充引文" in status for status in statuses))

    def test_serialized_review_budget_splits_units_without_trimming_original_blocks(self):
        model = ReviewModel(separate=True, content="Supporting explanation. " * 35)
        engine = SummaryEngine(model)
        engine.REVIEW_CHARS = 1600
        bundle = engine.summarize([SourceBlock("a", "The first complete original passage."),
                                   SourceBlock("b", "The second complete original passage.")],
                                  kind="fixture", title="Budget grouping")
        reviews = model.reviews()
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0]["blocks"], reviews[1]["blocks"])
        self.assertEqual(reviews[0]["blocks"], model.inputs[0]["blocks"])
        self.assertTrue(all(len(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
                            <= engine.REVIEW_CHARS for data in reviews))
        self.assertCountEqual([unit["id"] for data in reviews for unit in data["units"]], ["k1", "k2"])
        self.assertEqual(bundle.processing["source_review_batches"], 2)

    def test_review_budget_and_existing_total_call_limit_fail_before_an_unbounded_request(self):
        for limit_name, limit in (("REVIEW_CHARS", 100), ("MAX_CALLS", 2)):
            with self.subTest(limit=limit_name):
                model = ReviewModel()
                engine = SummaryEngine(model)
                setattr(engine, limit_name, limit)
                with self.assertRaises(SummaryError):
                    engine.summarize([SourceBlock("source", "Complete source text.")], kind="fixture", title="Budget")
                self.assertEqual(len(model.inputs), 2)
                self.assertEqual(model.reviews(), [])

    def test_separately_bounded_reviews_cannot_expand_the_total_knowledge_budget(self):
        def expand(data, result):
            result["units"][0]["content"] = "A fuller explanation. " * 70
            return result

        model = ReviewModel(review=expand, separate=True, content="Initial explanation. " * 35)
        engine = SummaryEngine(model)
        engine.REVIEW_CHARS = 1600
        engine.OUTPUT_CHARS = 2500
        with self.assertRaisesRegex(SummaryError, "核对后的知识超过"):
            engine.summarize([SourceBlock("a", "The first passage."), SourceBlock("b", "The second passage.")],
                             kind="fixture", title="Aggregate output budget")
        self.assertEqual(len(model.reviews()), 2)

    def test_cancellation_before_review_prevents_the_next_request(self):
        token = CancellationToken()
        model = ReviewModel()

        def progress(message):
            if "核对知识" in message:
                token.cancel()

        with self.assertRaises(RunCancelled):
            self.summarize(model, [SourceBlock("source", "Material.")], cancellation=token, progress=progress)
        self.assertEqual(len(model.inputs), 2)
        self.assertEqual(model.reviews(), [])

    def test_cancellation_during_review_prevents_returning_a_completed_bundle(self):
        token = CancellationToken()

        def cancel(data, result):
            token.cancel()
            return result

        model = ReviewModel(review=cancel)
        with self.assertRaises(RunCancelled):
            self.summarize(model, [SourceBlock("source", "Material.")], cancellation=token)
        self.assertEqual(len(model.reviews()), 1)
        self.assertIs(model.cancellations[-1], token)


if __name__ == "__main__":
    unittest.main()
