import unittest

from app.discussion import ReplyTurn, direct_addressees, reply_turns


class ReplyTurnTests(unittest.TestCase):
    def test_an_explicit_question_only_requires_the_named_ai(self):
        for question in (
            "Player A, how would you handle a disagreement?",
            "How would you handle a disagreement, Player A?",
            "Could Player A explain how to handle a disagreement",
        ):
            with self.subTest(question=question):
                self.assertEqual(reply_turns(question, ("B", "A")), (ReplyTurn("A", True, True),))
        self.assertEqual(reply_turns("Player C, what do you think?", ("A", "B")), ())

    def test_greeting_and_multiple_questions_keep_the_named_recipient(self):
        text = "Hey player C, are you honest?? and what factors you consider for a decision making?"
        self.assertEqual(direct_addressees(text), {"C"})
        self.assertEqual(reply_turns(text, ("B", "C")), (ReplyTurn("C", True, True),))
        self.assertEqual(direct_addressees("What do you think of Player C's suggestion?"), set())

    def test_a_question_to_both_requires_a_turn_for_each(self):
        for question in (
            "Player A and Player B, how would you break a tie?",
            "Players A and B, how would you break a tie?",
        ):
            with self.subTest(question=question):
                self.assertEqual(reply_turns(question, ("B", "A")), (
                    ReplyTurn("B", True, True), ReplyTurn("A", True, True),
                ))

    def test_a_presence_question_calls_the_missing_player(self):
        for question in (
            "where is player c?",
            "Where's Player C now?",
            "Is Player C still here?",
            "Player C, are you there?",
        ):
            with self.subTest(question=question):
                self.assertEqual(reply_turns(question, ("A", "C")), (ReplyTurn("C", True, True),))
        self.assertEqual(reply_turns("where is player c?", ("A", "B")), ())

    def test_a_general_question_has_one_responder(self):
        for question in (
            "How should we compare different approaches?",
            "How should we evaluate Player A's proposal?",
            "Player A's idea has merit. How might we apply it?",
            "What do you think of Player A?",
            "Where is Player A's argument weakest?",
        ):
            with self.subTest(question=question):
                self.assertEqual(reply_turns(question, ("B", "A")), (ReplyTurn("B", True),))

    def test_an_explicit_group_request_offers_a_second_response(self):
        for question in (
            "Both players, how would you break a tie?",
            "What do both of you think about a random draw?",
            "Can everyone give an example?",
        ):
            with self.subTest(question=question):
                self.assertEqual(reply_turns(question, ("B", "A")), (
                    ReplyTurn("B", True), ReplyTurn("A", False),
                ))

    def test_self_nomination_requires_one_response_even_without_punctuation(self):
        for message in (
            "i want to become the decision maker please vot me",
            "I’d like to serve as the Decision Maker",
            "Please consider me for decision-maker",
        ):
            with self.subTest(message=message):
                self.assertEqual(reply_turns(message, ("B", "A")), (ReplyTurn("B", True),))
        self.assertEqual(reply_turns("Player A, I want to be the decision maker", ("B", "A")), (
            ReplyTurn("A", True, True),
        ))

    def test_ordinary_chat_and_typos_get_a_primary_reply(self):
        for message in (
            "We could each share our priorities first.",
            "please give me vote guys",
            "i want the votes",
            "vote me in this round",
            "plz vote me",
            "vote meeeee",
            "hey",
            "hlo?",
            "helo??",
            "hello?",
            "hello???? i am saying that i would vote according to hjonesty",
        ):
            with self.subTest(message=message):
                self.assertEqual(reply_turns(message, ("A", "B")), (ReplyTurn("A", True),))
        self.assertEqual(reply_turns("Player A, I like your suggestion.", ("A", "B")), (
            ReplyTurn("A", True, True),
        ))
        self.assertEqual(reply_turns("Any thoughts?", ()), ())
