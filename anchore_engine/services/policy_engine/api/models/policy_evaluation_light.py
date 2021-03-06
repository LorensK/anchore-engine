# coding: utf-8

from __future__ import absolute_import
from datetime import date, datetime  # noqa: F401

from typing import List, Dict  # noqa: F401

from anchore_engine.services.policy_engine.api.models.base_model_ import Model
from anchore_engine.services.policy_engine.api.models.policy import Policy  # noqa: F401,E501
from anchore_engine.services.policy_engine.api.models.policy_evaluation_problem import PolicyEvaluationProblem  # noqa: F401,E501
from anchore_engine.services.policy_engine.api.models.whitelist import Whitelist  # noqa: F401,E501
from anchore_engine.services.policy_engine.api import util


class PolicyEvaluationLight(Model):
    """NOTE: This class is auto generated by the swagger code generator program.

    Do not edit the class manually.
    """

    def __init__(self, timestamp=None, image_id=None, policy=None, whitelists=None, evaluation_result=None, evaluation_problems=None):  # noqa: E501
        """PolicyEvaluationLight - a model defined in Swagger

        :param timestamp: The timestamp of this PolicyEvaluationLight.  # noqa: E501
        :type timestamp: str
        :param image_id: The image_id of this PolicyEvaluationLight.  # noqa: E501
        :type image_id: str
        :param policy: The policy of this PolicyEvaluationLight.  # noqa: E501
        :type policy: Policy
        :param whitelists: The whitelists of this PolicyEvaluationLight.  # noqa: E501
        :type whitelists: List[Whitelist]
        :param evaluation_result: The evaluation_result of this PolicyEvaluationLight.  # noqa: E501
        :type evaluation_result: object
        :param evaluation_problems: The evaluation_problems of this PolicyEvaluationLight.  # noqa: E501
        :type evaluation_problems: List[PolicyEvaluationProblem]
        """
        self.swagger_types = {
            'timestamp': str,
            'image_id': str,
            'policy': Policy,
            'whitelists': List[Whitelist],
            'evaluation_result': object,
            'evaluation_problems': List[PolicyEvaluationProblem]
        }

        self.attribute_map = {
            'timestamp': 'timestamp',
            'image_id': 'image_id',
            'policy': 'policy',
            'whitelists': 'whitelists',
            'evaluation_result': 'evaluation_result',
            'evaluation_problems': 'evaluation_problems'
        }

        self._timestamp = timestamp
        self._image_id = image_id
        self._policy = policy
        self._whitelists = whitelists
        self._evaluation_result = evaluation_result
        self._evaluation_problems = evaluation_problems

    @classmethod
    def from_dict(cls, dikt):
        """Returns the dict as a model

        :param dikt: A dict.
        :type: dict
        :return: The PolicyEvaluationLight of this PolicyEvaluationLight.  # noqa: E501
        :rtype: PolicyEvaluationLight
        """
        return util.deserialize_model(dikt, cls)

    @property
    def timestamp(self):
        """Gets the timestamp of this PolicyEvaluationLight.


        :return: The timestamp of this PolicyEvaluationLight.
        :rtype: str
        """
        return self._timestamp

    @timestamp.setter
    def timestamp(self, timestamp):
        """Sets the timestamp of this PolicyEvaluationLight.


        :param timestamp: The timestamp of this PolicyEvaluationLight.
        :type timestamp: str
        """

        self._timestamp = timestamp

    @property
    def image_id(self):
        """Gets the image_id of this PolicyEvaluationLight.


        :return: The image_id of this PolicyEvaluationLight.
        :rtype: str
        """
        return self._image_id

    @image_id.setter
    def image_id(self, image_id):
        """Sets the image_id of this PolicyEvaluationLight.


        :param image_id: The image_id of this PolicyEvaluationLight.
        :type image_id: str
        """

        self._image_id = image_id

    @property
    def policy(self):
        """Gets the policy of this PolicyEvaluationLight.


        :return: The policy of this PolicyEvaluationLight.
        :rtype: Policy
        """
        return self._policy

    @policy.setter
    def policy(self, policy):
        """Sets the policy of this PolicyEvaluationLight.


        :param policy: The policy of this PolicyEvaluationLight.
        :type policy: Policy
        """

        self._policy = policy

    @property
    def whitelists(self):
        """Gets the whitelists of this PolicyEvaluationLight.


        :return: The whitelists of this PolicyEvaluationLight.
        :rtype: List[Whitelist]
        """
        return self._whitelists

    @whitelists.setter
    def whitelists(self, whitelists):
        """Sets the whitelists of this PolicyEvaluationLight.


        :param whitelists: The whitelists of this PolicyEvaluationLight.
        :type whitelists: List[Whitelist]
        """

        self._whitelists = whitelists

    @property
    def evaluation_result(self):
        """Gets the evaluation_result of this PolicyEvaluationLight.


        :return: The evaluation_result of this PolicyEvaluationLight.
        :rtype: object
        """
        return self._evaluation_result

    @evaluation_result.setter
    def evaluation_result(self, evaluation_result):
        """Sets the evaluation_result of this PolicyEvaluationLight.


        :param evaluation_result: The evaluation_result of this PolicyEvaluationLight.
        :type evaluation_result: object
        """

        self._evaluation_result = evaluation_result

    @property
    def evaluation_problems(self):
        """Gets the evaluation_problems of this PolicyEvaluationLight.

        list of error objects indicating errors encountered during evaluation execution  # noqa: E501

        :return: The evaluation_problems of this PolicyEvaluationLight.
        :rtype: List[PolicyEvaluationProblem]
        """
        return self._evaluation_problems

    @evaluation_problems.setter
    def evaluation_problems(self, evaluation_problems):
        """Sets the evaluation_problems of this PolicyEvaluationLight.

        list of error objects indicating errors encountered during evaluation execution  # noqa: E501

        :param evaluation_problems: The evaluation_problems of this PolicyEvaluationLight.
        :type evaluation_problems: List[PolicyEvaluationProblem]
        """

        self._evaluation_problems = evaluation_problems
