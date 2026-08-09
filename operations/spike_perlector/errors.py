"""Named refusals for the reading-claim measurement instrument."""


class MeasurementRefusal(ValueError):
    """A proposed measurement is not sufficiently specified to run honestly."""


class PromptFidelityRefusal(MeasurementRefusal):
    """The harness cannot prove it supplied the candidate's declared prompt bytes."""


class HoldoutRefusal(MeasurementRefusal):
    """Evaluation material was proposed for a prohibited later use."""


class DisclosureRefusal(MeasurementRefusal):
    """A third-party transmission lacks its exact recorded approval."""


class MatrixRefusal(MeasurementRefusal):
    """A full candidate × act × condition matrix cannot be constructed."""


class PublicSafetyRefusal(MeasurementRefusal):
    """A proposed public finding contains a field outside its safe schema."""


class CandidateRosterRefusal(MeasurementRefusal):
    """A candidate roster violates the settled Perlector constraints."""


class AdjudicationRefusal(MeasurementRefusal):
    """Two transcriptions could not become a checked reference this instrument trusts."""
