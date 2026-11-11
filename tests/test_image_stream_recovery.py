import copy
import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from curl_cffi import requests
from PIL import Image

from services.image_generation_stream import _completed_messages
from services.image_generation_stream import stream_image_generation
from services.protocol import conversation as conv
from services.protocol.final_image import FinalImageTracker
from tests.test_image_generation_stream import LocalStreamServer, CONVERSATION_ID


CID = '11111111-1111-4111-8111-111111111111'


def document():
    return {'conversation_id': CID, 'messages': [
        {'id': 'submitted', 'author': {'role': 'user'},
         'content': {'content_type': 'text', 'parts': ['draw']}, 'metadata': {}},
        {'id': 'image', 'author': {'role': 'tool', 'name': 'image_generation'},
         'status': 'finished_successfully', 'metadata': {'image_gen_title': 'test'},
         'content': {'content_type': 'multimodal_text', 'parts': [
             {'content_type': 'image_asset_pointer', 'asset_pointer': 'file-service://file-result'}]}},
        {'id': 'final', 'author': {'role': 'assistant'}, 'recipient': 'all', 'channel': 'final',
         'status': 'finished_successfully', 'end_turn': True, 'metadata': {},
         'content': {'content_type': 'text', 'parts': ['']}},
    ]}


class RecoveryScopeTests(unittest.TestCase):
    def test_only_original_complete_turn_is_accepted(self):
        value = document()
        self.assertEqual(['submitted', 'image', 'final'],
                         [m['id'] for m in _completed_messages(value, CID, 'submitted')])
        self.assertIsNone(_completed_messages(value, 'different', 'submitted'))
        self.assertIsNone(_completed_messages(value, CID, 'different'))

    def test_incomplete_or_failed_turn_never_recovers_initial_image(self):
        for mutation in ['pending', 'failure', 'next_user', 'pagination', 'async', 'no_image', 'active_tool']:
            with self.subTest(mutation=mutation):
                value = document()
                if mutation == 'pending': value['messages'][-1]['end_turn'] = False
                if mutation == 'failure': value['messages'][-1]['status'] = 'failed'
                if mutation == 'next_user':
                    value['messages'].append({**copy.deepcopy(value['messages'][0]), 'id': 'later-user'})
                if mutation == 'pagination': value['page_info'] = {'has_previous_page': True}
                if mutation == 'async': value['async_status'] = 'running'
                if mutation == 'no_image': value['messages'].pop(1)
                if mutation == 'active_tool': value['messages'][1]['status'] = 'in_progress'
                self.assertIsNone(_completed_messages(value, CID, 'submitted'))

    def test_old_completed_branch_is_not_recovered(self):
        value = document()
        value['mapping'] = {
            m['id']: {'message': m, 'parent': parent}
            for m, parent in zip(value['messages'], [None, 'submitted', 'image'])
        }
        value['mapping']['pending'] = {'parent': 'submitted', 'message': {
            'id': 'pending', 'author': {'role': 'assistant'},
            'content': {'content_type': 'text', 'parts': ['working']},
            'status': 'in_progress', 'end_turn': False}}
        value['current_node'] = 'pending'
        self.assertIsNone(_completed_messages(value, CID, 'submitted'))

    def test_cached_snapshot_cannot_discard_already_observed_work(self):
        state = FinalImageTracker()
        state.update({'message': {**document()['messages'][1], 'id': 'newer-image'}})
        self.assertIsNone(_completed_messages(document(), CID, 'submitted', observed=state))
        self.assertIsNone(_completed_messages(
            document(), CID, 'submitted', required_tool_calls={'new-edit-call': 'python'}))


class RecoveryAccountingTests(unittest.TestCase):
    def test_recovered_final_image_is_delivered_and_settled_once(self):
        server = LocalStreamServer([{'conversation_id': CONVERSATION_ID}])
        self.addCleanup(server.finish)
        session = requests.Session(trust_env=False)
        self.addCleanup(session.close)
        image = io.BytesIO()
        Image.new('RGB', (8, 6), 'blue').save(image, format='PNG')
        png = image.getvalue()
        backend = SimpleNamespace(
            base_url=server.base_url, session=session, _image_submitted_message_id='submitted',
            image_conversation_id='', _webimage_generation_sent=True, _voice_image_session=None,
            download_final_sandbox_images=Mock(return_value=[png]), close=Mock(),
        )
        backend.stream_conversation = lambda **kwargs: stream_image_generation(
            backend, {'messages': [{'id': 'submitted'}]}, {}, .4)
        accounts = Mock()
        accounts.get_available_access_token.return_value = 'original-synthetic-token'
        accounts.get_account.return_value = {'email': 'test@example.test'}
        request = conv.ConversationRequest(model='gpt-image-2', prompt='synthetic',
                                           image_transport='conversation', response_format='b64_json')
        with patch.object(conv, 'OpenAIBackendAPI', return_value=backend) as factory, \
             patch.object(conv, 'account_service', accounts), \
             patch('services.image_generation_stream.IMAGE_STREAM_IDLE_SECS', .03):
            results = conv._generate_single_image_impl(request, 1, 1)
        result = next(r for r in results if r.kind == 'result')
        self.assertEqual(png, base64.b64decode(result.data[0]['b64_json']))
        factory.assert_called_once_with(access_token='original-synthetic-token')
        accounts.get_available_access_token.assert_called_once()
        accounts.mark_image_result.assert_called_once_with('original-synthetic-token', True,
            generation_id=backend._image_generation_id, not_consumed=False)
        self.assertEqual(backend._image_generation_id, accounts.get_available_access_token.call_args.kwargs['generation_id'])
        backend.close.assert_called_once()
        self.assertEqual(1, server.post_count)
        self.assertTrue(server.peer_closed.wait(.3))


if __name__ == '__main__':
    unittest.main()
