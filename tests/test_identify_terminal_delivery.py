"""PNG identify 终态产物契约：脱敏文本夹具，不执行命令或访问上游。"""
import base64
import copy
import unittest

from tests import test_container_terminal_delivery as container_tests


OUTPUT_PATH = container_tests.OUTPUT_PATH
DEPRECATION_WARNING = (
    'WARNING: The convert command is deprecated in IMv7, use "magick" '
    'instead of "convert" or "magick convert"'
)


def identify_line(size='1.28936MiB', path=OUTPUT_PATH):
    return f'{path} PNG 1024x1024 1024x1024+0+0 8-bit sRGB {size} 0.000u 0:00.000'


def identify_output(size='1.28936MiB'):
    # 两条真实输出只在文件名和文件大小上不同；仅文件名已替换为合成路径。
    return f'{DEPRECATION_WARNING}\n\n{identify_line(size)}\n'


def identify_document(output=None):
    doc = container_tests.container_document(identify_output() if output is None else output)
    doc['messages'][2]['content']['text'] = (
        'convert /mnt/data/input.png -resize 1024x1024 /mnt/data/final.png\n'
        'identify /mnt/data/final.png'
    )
    return doc


class IdentifyTerminalDeliveryTests(unittest.TestCase):
    def test_accepts_observed_png_identify_output_without_mutation(self):
        for output in (identify_output('1.28936MiB'), identify_output('1.39211MiB'),
                       identify_line() + '\n'):
            for async_status in (None, 4):
                with self.subTest(output=output, async_status=async_status):
                    doc = identify_document(output)
                    doc['async_status'] = async_status
                    original = copy.deepcopy(doc)
                    result = container_tests.select(doc)
                    self.assertIsNotNone(result)
                    self.assertEqual(
                        ('container_output', 'container-result', OUTPUT_PATH, None),
                        (result.kind, result.message_id, result.path, result.image_message),
                    )
                    self.assertEqual(original, doc)

    def test_rejects_natural_language_partial_records_and_extra_output(self):
        for output in (
            f'Saved image: {identify_line()}',
            f'The final image is {OUTPUT_PATH}',
            f'{identify_line()}\nDone',
            f'Process exited with code 0\n{identify_line()}',
            f'{OUTPUT_PATH} PNG 1024x1024',
            f'{OUTPUT_PATH} PNG 1024x1024 1024x1024+0+0 8-bit sRGB',
            identify_line() + ' extra',
        ):
            with self.subTest(output=output):
                self.assertIsNone(container_tests.select(identify_document(output)))

    def test_rejects_multiple_records_or_additional_paths(self):
        for output in (
            f'{identify_line()}\n{identify_line(path="/mnt/data/other.png")}',
            f'{identify_line()}\n{identify_line()}',
            f'{identify_line()}\n/mnt/data/other.png',
            f'/mnt/data/other.png\n{identify_line()}',
            f'{identify_line()} /mnt/data/other.png',
        ):
            with self.subTest(output=output):
                self.assertIsNone(container_tests.select(identify_document(output)))

    def test_rejects_errors_and_unrecognized_warning_text(self):
        for output in (
            f'WARNING: conversion incomplete\n\n{identify_line()}',
            f'{DEPRECATION_WARNING}\nWARNING: image truncated\n{identify_line()}',
            f'{DEPRECATION_WARNING} unexpected suffix\n\n{identify_line()}',
            f'ERROR: failed to write output\n{identify_line()}',
            f'Traceback: error\n{identify_line()}',
            f'{identify_line()}\nidentify: corrupt image',
            f'{identify_line()}\n{DEPRECATION_WARNING}',
        ):
            with self.subTest(output=output):
                self.assertIsNone(container_tests.select(identify_document(output)))

    def test_rejects_invalid_paths_and_non_png_records(self):
        for path in (
            '/mnt/data/../secret.png', '/mnt/data/%2e%2e/secret.png',
            '/mnt/data/%252e%252e/secret.png', '/mnt/data/final.png?query=1',
            '/mnt/data/final.png\x00', '/mnt/data/final.txt', '/tmp/final.png',
        ):
            with self.subTest(path=path):
                self.assertIsNone(container_tests.select(identify_document(identify_line(path=path))))
        for output in (
            identify_line().replace(' PNG ', ' JPEG '),
            identify_line().replace(' PNG ', ' png '),
            identify_line().replace('1024x1024', '0x1024'),
            identify_line().replace('1024x1024', '-1x1024'),
        ):
            with self.subTest(output=output):
                self.assertIsNone(container_tests.select(identify_document(output)))

    def test_requires_exact_successful_aggregate_matching_identify_text(self):
        for key, value in (
            ('status', 'error'), ('status', 'running'), ('messages', None),
            ('messages', [{'message_type': 'stream', 'stream_name': 'stderr', 'text': 'error'}]),
            ('final_expression_output', OUTPUT_PATH),
            ('final_expression_output', identify_line() + '\n'),
            ('final_expression_output', identify_output() + '\n'),
            ('timeout_triggered', True), ('exit_code', 1), ('error', 'failed'),
        ):
            with self.subTest(key=key, value=value):
                doc = identify_document()
                doc['messages'][3]['metadata']['aggregate_result'][key] = value
                self.assertIsNone(container_tests.select(doc))
        for key in ('status', 'messages', 'final_expression_output'):
            with self.subTest(missing=key):
                doc = identify_document()
                del doc['messages'][3]['metadata']['aggregate_result'][key]
                self.assertIsNone(container_tests.select(doc))

    def test_requires_all_three_matching_binding_ids(self):
        for key in container_tests.BINDINGS:
            for index in (2, 3):
                for value in (None, 'other-turn'):
                    with self.subTest(key=key, index=index, value=value):
                        doc = identify_document()
                        doc['messages'][index]['metadata'][key] = value
                        self.assertIsNone(container_tests.select(doc))

    def test_requires_original_completed_turn_with_empty_final(self):
        for target, mutation in (
            (2, {'status': 'in_progress'}),
            (3, {'status': 'in_progress'}),
            (-1, {'end_turn': False}),
            (-1, {'content': {'content_type': 'text', 'parts': ['Still working']}}),
        ):
            with self.subTest(target=target, mutation=mutation):
                doc = identify_document()
                doc['messages'][target].update(mutation)
                self.assertIsNone(container_tests.select(doc))
        doc = identify_document()
        doc['async_status'] = 'in_progress'
        self.assertIsNone(container_tests.select(doc))
        doc = identify_document()
        del doc['messages'][1]
        self.assertIsNone(container_tests.select(doc))
        doc = identify_document()
        doc['messages'].insert(2, container_tests.message(
            'new-user', 'user', {'content_type': 'text', 'parts': ['next turn']},
        ))
        self.assertIsNone(container_tests.select(doc))
        self.assertIsNone(container_tests.select(identify_document(), expected_final_message_id='other-final'))

    def test_never_falls_back_to_earlier_identify_after_new_container_work(self):
        doc = identify_document()
        later = container_tests.container_document('No final image')['messages'][2:4]
        later[0]['id'] = 'later-container-call'
        later[1]['id'] = 'later-container-result'
        doc['messages'][4:4] = later
        self.assertIsNone(container_tests.select(doc))


class IdentifyTerminalPipelineTests(unittest.TestCase):
    def test_observed_identify_output_delivers_png_from_original_tool_message(self):
        harness = container_tests.ContainerTerminalPipelineTests()
        doc = identify_document()
        doc['async_status'] = 4
        backend, png = harness.backend(doc)
        results = [item for item in harness.run_pipeline(backend) if item.kind == 'result']
        self.assertEqual(1, len(results))
        self.assertEqual(png, base64.b64decode(results[0].data[0]['b64_json']))
        self.assertEqual('container_terminal', results[0].execution['result_source'])
        backend.download_final_sandbox_images.assert_called_once_with(
            container_tests.CONVERSATION_ID, 'container-result', [OUTPUT_PATH],
        )
        backend._resolve_image_urls.assert_not_called()
        backend.resolve_conversation_image_urls.assert_not_called()
        backend.download_image_bytes.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
