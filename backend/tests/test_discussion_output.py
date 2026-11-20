import unittest

from app.discussion_output import (
    discloses_vote, has_self_narration, is_near_duplicate, normalize_player_labels, public_chat_violation,
)


class DiscussionOutputTests(unittest.TestCase):
    def test_rejects_a_minor_rewording_of_the_last_contribution(self):
        previous = (
            "If neither option is clearly better, I would prioritize honesty and fairness by seeking additional "
            "information or consulting trusted sources to make an informed decision. It's important to remain "
            "transparent and avoid bias when uncertainty exists."
        )
        repeated = (
            "I would prioritize transparency and fairness by seeking additional information or consulting trusted "
            "sources to make an informed decision. It's important to remain honest and avoid bias when uncertainty exists."
        )
        self.assertTrue(is_near_duplicate(repeated, [previous]))
        self.assertFalse(is_near_duplicate(previous + " What would you do if more evidence never arrives?", [previous]))

    def test_self_voice_rejects_own_third_person_without_rejecting_other_players(self):
        for message in (
            "Player C should consider balancing perspectives and making fair decisions.",
            "Player C is honest in his considerations.",
            "Player C believes fairness matters.",
            "Player C’s response highlights the importance of listening.",
            "Candidate C would seek more evidence.",
        ):
            with self.subTest(message=message):
                self.assertTrue(has_self_narration(message, "C"))
        for message in (
            "I'd show honesty by admitting when I lack evidence.",
            "Player B should explain which evidence would change their mind.",
            "Player B, what would change your mind?",
            "Player C, are you honest?",
            'You asked, "Player C, are you honest?" I would admit uncertainty.',
            '"Player C should consider fairness" was your suggestion; I would also ask for reasons.',
            "As Player C, I'd hear both sides before deciding.",
        ):
            with self.subTest(message=message):
                self.assertFalse(has_self_narration(message, "C"))

    def test_near_duplicate_catches_minor_rewrites_and_changed_addresses(self):
        previous = ["I'd listen to both sides and explain my reasons before making a difficult decision."]
        for message in (
            previous[0],
            "Player A, I'd listen to both sides and explain my reasons before making a difficult decision!",
            "I'd listen to both sides and explain my reasons before making the difficult decision.",
            "I'd listen to both sides and explain my reasoning before making a difficult decision.",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_near_duplicate(message, previous))

    def test_near_duplicate_preserves_new_questions_and_concrete_additions(self):
        previous = ["I'd listen to both sides and explain my reasons before making a difficult decision."]
        for message in (
            previous[0] + " Which risks matter?",
            previous[0] + " I would ask someone to challenge the weakest assumption.",
            "Listening matters, but I'd also state what evidence could change my view.",
            "What if the two sides disagree about which evidence is reliable?",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_near_duplicate(message, previous))

    def test_near_duplicate_leaves_short_greetings_and_acknowledgments_alone(self):
        for message in ("Hello!", "I agree.", "Thanks, that helps."):
            with self.subTest(message=message):
                self.assertFalse(is_near_duplicate(message, [message]))
        self.assertFalse(is_near_duplicate("I'd ask the group to challenge the weakest assumption in my argument.", []))

    def test_normalizes_candidate_labels_without_changing_ordinary_uses(self):
        self.assertEqual(
            normalize_player_labels('Candidate A listened. CANDIDATE B told candidate c, "Ask player a."'),
            'Player A listened. Player B told Player C, "Ask Player A."',
        )
        self.assertEqual(
            normalize_player_labels("A candidate should hear everyone. Give each candidate a chance."),
            "A candidate should hear everyone. Give each candidate a chance.",
        )

    def test_flags_intended_and_reported_self_votes(self):
        for disclosure in (
            "I will vote for Candidate A.",
            "I'd vote for Player B because they listened.",
            "I would like to vote for myself.",
            "I'd love to vote for Player C.",
            "I hope to vote for Player A.",
            "I’m voting for you.",
            "I voted for myself.",
            "I plan to cast my ballot for Player C.",
            "I will cast a vote for Player C.",
            "I would give my vote to Player B.",
            "I think I'll vote for B.",
            "My vote goes to Player A.",
            "Player B has my vote.",
            '"Player B" has my vote.',
            "My vote: Player A",
            "I'm leaning toward Player C.",
            "I'm going with Player A.",
            "My choice is Player B.",
            "I'd choose Player A. What do you think?",
            '"I will vote for Player C."',
            'I will vote for "Player C".',
            "I care about honesty, so I'll vote for Player C.",
            "I voted in round 1 for Player A, Player B, and Player C.",
            "I already voted during the first round for Player B.",
            "I cast my ballot in round 1 for Player C.",
        ):
            with self.subTest(disclosure=disclosure):
                self.assertTrue(discloses_vote(disclosure))

    def test_allows_criteria_questions_and_comments_on_contributions(self):
        for message in (
            "I would vote based on who listened.",
            "I would vote for a candidate who listened.",
            "I would vote for whoever weighed both ideas.",
            "I prefer Player A's suggestion about letting everyone speak.",
            "Player A asked a fair question about honesty.",
            "Should I vote for Player A?",
            "Player A, can I vote for you?",
            "Why would I vote for Player A?",
            'Player A said, "I will vote for Player C." What reasoning did they give?',
            "I would look for someone who can explain a difficult decision in plain language.",
        ):
            with self.subTest(message=message):
                self.assertFalse(discloses_vote(message))

    def test_blocks_named_vote_instructions_without_blocking_general_criteria(self):
        for message in (
            "Vote for Player C.", "Please vote for Candidate A because they listened.",
            "Choose Player B.", "Support C.",
        ):
            with self.subTest(message=message):
                self.assertTrue(discloses_vote(message))
        for message in (
            "Vote for someone who explains their reasons.",
            "Choose a player who admits uncertainty.",
            'Player A said, "Vote for Player C." What was their reasoning?',
        ):
            with self.subTest(message=message):
                self.assertFalse(discloses_vote(message))

    def test_own_vote_uses_a_bounded_clause_not_a_modifier_whitelist(self):
        for message in (
            "I will proceed to vote for myself as the Decision Maker.",
            "I will proceed to vote for myself as the Decision Maker. Let's move forward with the public discussion.",
            "I will not vote for myself as the Decision Maker.",
            "I won't vote for myself.",
            "I have carefully considered the discussion and now intend to vote for Player B.",
            "I decided, after considering the chat, to vote for Player C.",
            "I shall definitely proceed to cast my ballot for Candidate C.",
            "I have now made up my mind to give my vote to Player A.",
            "I am planning, after hearing everyone, to vote for myself.",
            "I will avoid voting for Player C.",
            "I will not vote myself.",
            "I will proceed to choose myself as the Decision Maker. I believe this is the most appropriate choice given the current circumstances.",
            "I have carefully considered the discussion and now intend to select Player B.",
            "I will proceed to pick Player C.",
            "I will continue leaning toward Player A.",
        ):
            with self.subTest(message=message):
                self.assertEqual(public_chat_violation(message), "ballot_disclosure")

    def test_false_eligibility_is_distinct_from_private_ballot_choices(self):
        for message in (
            "I cannot vote for myself as the Decision Maker, as I am not eligible to vote in this process.",
            "I cannot vote for myself.",
            "I can't vote for myself.",
            "I'm unable to vote for myself.",
            "I am not eligible to vote in this process.",
            "Player C is not allowed to vote.",
            "I am not eligible to be the Decision Maker.",
            "Self-voting is forbidden.",
            "Self-votes are prohibited.",
            "Self-votes aren't allowed.",
            "Voting for yourself is not allowed.",
        ):
            with self.subTest(message=message):
                self.assertEqual(public_chat_violation(message), "incorrect_eligibility")

    def test_true_voting_rules_questions_and_reported_dialogue_are_preserved(self):
        for message in (
            "I can vote for myself.",
            "I am eligible to vote for myself.",
            "I'm allowed to vote for Player B.",
            "Everyone may vote for any of the three players, including themselves.",
            "Self-votes are not forbidden.",
            "Self-voting isn't prohibited.",
            "Self-votes are not banned.",
            "Each player may submit one ballot per round.",
            "I cannot vote yet, because discussion is still open.",
            "I cannot vote for myself until voting opens.",
            "I am not allowed to vote more than once per round.",
            "I am not allowed to cast another ballot this round.",
            "I can't vote for myself twice in one round.",
            "I am not allowed to vote for Player C more than once per round.",
            "I would choose a player who listens.",
            "Am I not eligible to vote?",
            "Can I vote for myself?",
            "I ask why Player A would vote for Player C.",
            "I wonder whether I'd vote for Player A.",
            "I understand why you voted for Player B.",
            "I can explain what I look for in a Decision Maker.",
            'Player A said, "I cannot vote for myself." Is that correct?',
            'Player A said, "I will proceed to vote for myself." What is their reasoning?',
            'You asked, "I am not eligible to vote?" All players are eligible.',
        ):
            with self.subTest(message=message):
                self.assertIsNone(public_chat_violation(message))
