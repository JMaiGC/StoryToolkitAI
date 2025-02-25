import os
import sys
import time
import json
import yaml
import subprocess
import platform

from threading import Thread

import torch
import storytoolkitai.integrations.mots_whisper as whisper
from whisper import tokenizer as whisper_tokenizer

from transformers import pipeline

import librosa
import soundfile

import tqdm

from pydantic import BaseModel
from typing import Optional

from storytoolkitai.core.logger import logger

from storytoolkitai.integrations.mots_resolve import MotsResolve

from storytoolkitai import USER_DATA_PATH

from .projects import Project, get_projects_from_path, ProjectUtils
from .transcription import Transcription, TranscriptionSegment, TranscriptionUtils
from .story import Story, StoryLine, StoryUtils
from .document import Document
from .processing_queue import ProcessingQueue
from .search import ToolkitSearch, SearchItem, TextSearch, VideoSearch, cv2
from .assistant import ToolkitAssistant, AssistantUtils
from .assistant import DEFAULT_SYSTEM_MESSAGE as ASSISTANT_DEFAULT_SYSTEM_MESSAGE
from .media import MediaUtils
from .speaker_diarization import detect_speaker_changes
from .timecode import sec_to_tc, tc_to_sec

from timecode import Timecode

import numpy as np

from .monitor import Monitor
from .videoanalysis import ClipIndex
from .media import MediaItem, VideoFileClip, AudioFileClip


def is_arm64_mac():
    return platform.system() == 'Darwin' and platform.machine() == 'arm64'


class NLE:
    """
    Use this class to store any data that is supposed to be shared with the NLE (for eg. Resolve)
    """

    # the current project name
    current_project = None

    # the current timeline dict
    current_timeline = None

    # a dict with the current timeline markers
    current_timeline_markers = None

    # the current timecode
    current_tc = None

    # the current timeline fps
    current_timeline_fps = None
    current_start_tc = None

    # the current bin
    current_bin = None

    # resolve specific attributes
    resolve_error = 0
    resolve = None
    resolve_poll_num = 0

    # this is used to suspend polling to avoid requests when the NLE might be busy
    suspend_polling = False

    @staticmethod
    def reset_all():

        NLE.current_timeline \
            = NLE.current_project \
            = NLE.current_timeline_markers \
            = NLE.current_tc \
            = NLE.current_timeline_fps \
            = NLE.current_start_tc \
            = NLE.current_bin \
            = None

    @staticmethod
    def is_connected():
        """
        Check if the NLE is connected
        """

        if NLE.resolve is None:
            return False
        else:
            return True


class Observer:
    def update(self, subject):
        pass


class NotificationMessage(BaseModel):
    # what gets logged
    message: str

    # what gets displayed
    display_message: str

    # the log level
    level: str

    # whether to include the exception info in the log or not
    exc_info: Optional[bool] = None

class NotificationService:
    """
    This takes care that any notification received gets dispatched to the right place on the UI

    Syntax example:
    To batch and send two notifications at once, use:
    NotificationService("message1", level="warning").add("message2", level="info").push()

    By default, the NotificationService pushes the messages to the logger.

    To push the notification to a specific receiver, use:
    NotificationService("message1", level="warning").to("window", window_object).push()

    To push a different message to the frontend, compared to what is logged, use:
    NotificationService("message1", display_message="message to display", level="warning").push()

    This is useful if you want to push notifications from the backend (model, controller, adapter etc.) to the frontend.

    """

    # use here to define the known receiver types
    RECEIVER_TYPES = ['window']

    def __init__(self, message=None, *, display_message=None, level="info", exc_info=None):

        # initialize the batch
        self.batch = []

        # if there's a message, add it to the batch
        if message:
            self.add_message(message, display_message=display_message, level=level, exc_info=exc_info)

        # but we can't have a formatted_message without a message
        elif display_message:
            raise ValueError('display_message passed to NotificationService without a message.')

        self.receivers = {}

    def add_message(self, message, *, display_message=None, level="info", exc_info=None) -> 'NotificationService':

        # add the message to the batch
        # if no formatted message was passed, use the message to display
        self.batch.append(
            NotificationMessage(
                message=message,
                display_message=display_message or message,
                level=level,
                exc_info=exc_info
            )
        )

        return self

    def to(self, receiver_type, receiver_reference) -> 'NotificationService':
        """
        This adds a recipient to the notification service (for e.g. a specific UI window)
        """

        if receiver_type not in self.RECEIVER_TYPES:
            raise ValueError('Notification receiver type: {} not in list of known types: {}'
                             .format(receiver_type, self.RECEIVER_TYPES))

        if receiver_type not in self.receivers:
            self.receivers[receiver_type] = []

        # add the receiver reference
        # this should be an actual object that has a receive_notification() method!
        self.receivers[receiver_type].append(receiver_reference)

        return self

    def _process_message(self, notification_message: NotificationMessage) -> bool:
        """
        Processes a single notification message
        """

        # by processing we mean, first logging the message and then dispatching it to all the receiver
        try:
            if notification_message.level == 'error':
                logger.error(notification_message.message, exc_info=notification_message.exc_info)

            elif notification_message.level == 'warning':
                logger.warning(notification_message.message, exc_info=notification_message.exc_info)

            elif notification_message.level == 'debug':
                logger.debug(notification_message.message, exc_info=notification_message.exc_info)

            else:
                logger.info(notification_message.message, exc_info=notification_message.exc_info)

            # important: the receivers should be actual objects that have a receive_notification() method!
            # for e.g., if the receiver is a window object, it should have a window.receive_notification() method
            for receiver_type, receiver_list in self.receivers.items():
                for receiver in receiver_list:

                    # here, we send the full NotificationMessage object to the receiver
                    # the receiver has to decide which part of the message to use
                    receiver.receive_notification(notification_message)

        except Exception as e:
            logger.error('Error processing notification message: {}'.format(e))
            logger.debug("Error:", exc_info=True)

            return False

        return True

    def push(self) -> 'NotificationService':
        """
        Processes all the notification messages in order
        """
        try:
            for notification_message in self.batch:
                self._process_message(notification_message)
        except Exception as e:
            logger.error('Error processing notification messages: {}'.format(e))
            logger.debug("Error:", exc_info=True)

        return self

class ToolkitOps:

    def __init__(self, stAI=None, disable_resolve_api=False):

        # this will be used to store all the transcripts that are ready to be transcribed
        self.transcription_queue = {}

        # keep a reference to the StoryToolkitAI object here if one was passed
        self.stAI = stAI

        # initialize the toolkit search engine
        self.t_search_obj = ToolkitSearch(toolkit_ops_obj=self)

        # this is used to get fast the name of what is being transcribed currently
        self.transcription_queue_current_name = None

        # this is to keep track of the current transcription item
        # the format is {queue_id: transcription_item_attributes}
        self.transcription_queue_current_item = {}

        # todo: remove this and use observers instead
        # declare this as none for now so we know it exists
        self.toolkit_UI_obj = None

        # use this to store the whisper model later
        self.whisper_model = None

        # load the whisper model from the config
        # we're recommending the medium model for better accuracy vs. time it takes to process
        # if in doubt use the large model but that will need more time
        self.whisper_model_name = self.stAI.get_app_setting(setting_name='whisper_model_name', default_if_none='medium')

        # get the whisper device setting
        # currently, the setting may be cuda, cpu or auto
        self.torch_device = stAI.get_app_setting('torch_device', default_if_none='auto')

        self.torch_device = self.torch_device_type_select(self.torch_device)

        # now let's deal with the sentence transformer model
        # this is the transformer model name that we will use to search semantically
        self.s_semantic_search_model_name \
            = self.stAI.get_app_setting(setting_name='s_semantic_search_model_name',
                                        default_if_none='msmarco-distilbert-base-v4')

        # for now define an empty model here which should be loaded the first time it's needed
        # it's very likely that the model will not be loaded here, but in the SearchItem, for each search
        self.s_semantic_search_model = None

        # add observers so that we can trigger certain actions when something else happens
        # this dictionary will hold all the actions and their observers (for e.g. from the UI)
        self._observers = {}

        self.processing_queue = ProcessingQueue(toolkit_ops_obj=self)

        # this is used by the queue dispatcher to know which functions to call depending on the task
        # the key is the name of the task, the value is a list of functions to call for that task
        # the queue dispatcher may also merge multiple tasks into one (for eg. if transcribe+ingest is called)
        self.queue_tasks = {
            'transcribe': [self.whisper_transcribe],
            'translate': [self.whisper_transcribe],
            'group_questions': [self.group_questions],
            'index_text': [self.index_text],
            'index_video': [self.index_video],
            'speaker_detection': [self.speaker_detection]
        }

        # use this to store all the devices that can be used for processing queue tasks
        self.queue_devices = self.get_torch_available_devices()

        # use this to know whether the resolve API is disabled or not for this session
        self.disable_resolve_api = disable_resolve_api

        # if this is True, it means that there is a polling thread running
        self.polling_resolve = False

        # to hold the resolve API object
        self.resolve_api = None

        # init Resolve but if...

        # ... if --noresolve was passed as an argument, disable the resolve API
        if '--noresolve' in sys.argv:
            self.resolve_api = None
            self.disable_resolve_api = True
            logger.debug('Resolve API disabled via --noresolve argument.')

        # ... and if the resolve API is disabled via config, disable the resolve API
        elif self.stAI.get_app_setting('disable_resolve_api', default_if_none=True):
            self.resolve_api = None
            self.disable_resolve_api = True
            logger.debug('Resolve API disabled via config.')

        # ... then, init Resolve
        if not self.disable_resolve_api:
            self.resolve_enable()

        # if this is not the CLI
        # resume the transcription queue if there's anything in it
        if self.stAI.cli_args and self.stAI.cli_args.mode != 'cli' and self.processing_queue.resume_queue_from_file():
            logger.info('Resuming queue from file')

    def attach_observer(self, action, observer):
        """
        Attach an observer to an action
        """

        if action not in self._observers:
            self._observers[action] = []

        # add the observer to the list of observers for this action
        self._observers[action].append(observer)

    def dettach_observer(self, action, observer):
        """
        Dettach an observer from an action
        """

        if action not in self._observers:
            return False

        # remove the observer from the list of observers for this action
        self._observers[action].remove(observer)

        # if the list is empty, remove the action
        if len(self._observers[action]) == 0:
            del self._observers[action]

    def notify_observers(self, action):
        """
        Use this to notify all observers if a certain action has been performed
        """

        # no observers for this action
        if action not in self._observers:
            return False

        # notify all observers for this action
        for observer in self._observers[action]:
            observer.update()

    def get_torch_available_devices(self) -> list or None:

        # prepare a list of available devices
        available_devices = ['cpu']

        # and add cuda to the available devices, if it is available
        if torch.cuda.is_available():
            available_devices.append('CUDA')

        return available_devices

    # TRANSCRIPTION PROCESS MANAGEMENT

    def get_all_valid_media_paths_in_dir(self, dir_path, recursive=False):

        # if the source file path is a directory, get all the valid media files in the directory
        if not os.path.isdir(dir_path):
            logger.warning('The source file path is not a directory. Aborting.')
            return False

        # if the source file path is a file, return the file path
        elif os.path.isdir(dir_path):

            # get all the files in the directory
            # either recursively
            if not recursive:
                all_files = os.listdir(dir_path)
            else:
                all_files = []
                reached_limit = False
                for root, dirs, files in os.walk(dir_path):
                    for file in files:
                        all_files.append(os.path.join(root, file))

                        if len(all_files) > int(self.stAI.get_app_setting('ingest_file_limit', default_if_none=30)):
                            logger.warning('Going over the ingest files limit. Stopping at {} files.'
                                           .format(len(all_files)))

                            reached_limit = True
                            break

                    if reached_limit:
                        break

            # filter out the valid media files
            valid_media_files \
                = [os.path.join(dir_path, file) for file in all_files if self.is_valid_media_file(file)]

            return valid_media_files

        # if the source file path is neither a file nor a directory, return False
        return False

    def is_valid_media_file(self, source_file_path):
        """
        This checks if the source file path is a valid media file by checking the extension
        """

        if not source_file_path:
            return False

        # get the file extension
        file_extension = os.path.splitext(source_file_path)[1].lower()

        # check if the file extension is valid
        if file_extension in ['.mov', '.mp4', '.mp3', '.wav', '.aif', '.aiff']:
            return True

        # if the file extension is not valid, return False
        return False

    def add_media_to_queue(self, source_file_paths: str or list = None, queue_id: str = None,
                           transcription_settings=None, video_indexing_settings=None,
                           **kwargs):
        """
        This adds one media item to the ingest queue
        (the task however might split into multiple queue items, for eg. transcription and video indexing)
        """

        # if no source file path was passed, return False
        if not source_file_paths:
            fail_error = 'No source file path was passed for ingest. Aborting.'
            logger.warning(fail_error)

            # update the queue item status
            if queue_id is not None:
                self.processing_queue.update_queue_item(queue_id=queue_id, status='failed', fail_error=fail_error)

            return False

        # if the source file path is a string, convert it to a list
        if isinstance(source_file_paths, str):
            source_file_paths = [source_file_paths]

        # this is the path variable we'll use to send the items to the queue
        valid_source_file_paths = []

        # loop through the source file paths
        for source_file_path in source_file_paths:

            # check if the source file path exists
            if not os.path.exists(source_file_path):
                # if it doesn't exist, log a warning and go to the next path
                logger.warning('Source file path does not exist: ' + source_file_path)
                continue

            # if it's a DIRECTORY, add all the valid media files in the folder to the queue
            if os.path.isdir(source_file_path):

                # get all the valid media files in the folder
                valid_source_file_paths += self.get_all_valid_media_paths_in_dir(source_file_path, recursive=True)

            # if it's a FILE, check if it's a valid media file
            if os.path.isfile(source_file_path):

                # if it's a valid media file, add it to the queue
                if self.is_valid_media_file(source_file_path):
                    valid_source_file_paths.append(source_file_path)

                # if it's not a valid media file, log a warning and go to the next path
                else:
                    logger.debug('Skipping file path - not a valid media file: ' + source_file_path)
                    continue

        # if there are no valid source file paths, return False
        if not valid_source_file_paths:
            return False

        # this will hold all the queue ids generated in this call
        queued = []

        # if there are valid source file paths, add each of them to the queue
        for source_file_path in valid_source_file_paths:

            logger.debug('Reading {} to add to the queue.'.format(source_file_path))

            # check if there are audio streams in the file
            has_audio = MediaItem.has_audio(source_file_path)

            logger.debug('File {} {} a valid audio stream.'
                         .format(source_file_path, 'has' if has_audio else 'does not have'))

            # check if there are valid video streams in the file
            has_video = MediaItem.has_video(source_file_path)

            logger.debug('File {} {} a valid video stream.'
                         .format(source_file_path, 'has' if has_video else 'does not have'))

            # these two will hold all the queue ids of created for this file
            transcription_queue_ids = []
            video_indexing_queue_ids = []

            # create a transcription job only if we have transcription settings
            if has_audio and transcription_settings is not None and isinstance(transcription_settings, dict):

                transcription_settings['name'] = os.path.basename(source_file_path)

                # don't forget the queue_id
                transcription_settings['queue_id'] = queue_id

                # reset the queue id once we used it to prevent other items in this batch from using it
                queue_id = None

                # add the audio file path to the transcription settings
                transcription_settings['audio_file_path'] = source_file_path

                # send the item to transcription queue
                transcription_queue_id = self.add_transcription_to_queue(**transcription_settings)

                # add this to the queued list
                if isinstance(transcription_queue_id, str):
                    queued.append(transcription_queue_id)
                    transcription_queue_ids.append(transcription_queue_id)

                elif isinstance(transcription_queue_id, list):
                    queued.extend(transcription_queue_id)
                    transcription_queue_ids.extend(transcription_queue_id)

                # throttle a bit to avoid collisions etc.
                time.sleep(0.05)

            # create a video indexing job only if we have video indexing settings
            if has_video and video_indexing_settings is not None and isinstance(video_indexing_settings, dict):

                video_indexing_settings['name'] = os.path.basename(source_file_path)

                # add the video file path to the transcription settings
                video_indexing_settings['video_file_path'] = source_file_path

                # add the current queue id to the video indexing settings
                video_indexing_settings['queue_id'] = queue_id

                # reset the queue id once we used it to prevent other items in this batch from using it
                queue_id = None

                # add the indexing to the queue
                current_queue_id = self.add_index_video_to_queue(**video_indexing_settings)

                # add the generated queue id to the list to the queued list
                queued.append(current_queue_id)
                video_indexing_queue_ids.append(current_queue_id)

            # if there is no video, but the file looks like a video file, log this
            elif not has_video \
                and (source_file_path.endswith('.mp4')
                     or source_file_path.endswith('.mov')
                     or source_file_path.endswith('.avi')):

                logger.debug('Skipping video indexing for file {} - no video streams found or codec unknown.'
                             .format(source_file_path))

            # if we have both audio and video indexing queue ids,
            # we need to let the transcription queue items know about the video indexing queue ids
            if transcription_queue_ids and video_indexing_queue_ids:

                # get all the transcription queue items that we just added
                for transcription_queue_id in transcription_queue_ids:

                    # add the video indexing queue id to the transcription queue item
                    self.processing_queue.update_queue_item(
                        transcription_queue_id, video_indexing_queue_ids=video_indexing_queue_ids)

        # confirm that stuff was added to the queue
        if len(queued) > 0:
            return queued

        # otherwise return false
        return False

    def add_transcription_to_queue(self, transcription_task=None, audio_file_path: str = None, queue_id: str = None,
                                   **kwargs):
        """
        This adds a transcription item to the transcription queue
        (it also splits it into two queue items, if it's a "transcribe+translate" transcription_task)

        Here, we are processing the options that have something to do with the queue processing
        The whisper options are processed by the whisper_transcribe function,
        since we might still need them in the queue history later
        """

        # if no audio file path was passed, return False
        if not audio_file_path:
            fail_error = 'No audio file path was passed for transcription. Aborting.'
            logger.warning(fail_error)

            # update the queue item status
            if queue_id is not None:
                self.processing_queue.update_queue_item(queue_id=queue_id, status='failed', fail_error=fail_error)

            return False

        # as name, use either the passed name or the file name if nothing was passed
        name = kwargs.get('name', os.path.basename(audio_file_path))

        # get the right device via torch_device_type_select
        kwargs['device'] = self.torch_device_type_select(kwargs.get('device', None))

        # select the 'transcribe' if neither transcribe or translate was passed
        if transcription_task is None \
                or transcription_task not in ['transcribe', 'translate', 'transcribe+translate']:
            transcription_task = 'transcribe'

        # only allow 'transcribe' or 'translate'
        if transcription_task in ['transcribe', 'translate']:

            # just do that task
            transcription_tasks = [transcription_task]

        # if user asked for 'transcribe+translate'
        # split this into two items to add them separately to the queuee
        elif transcription_task == 'transcribe+translate':
            # add both tasks
            transcription_tasks = ['transcribe', 'translate']

        # we will never get to this, but let's have it
        else:
            return False

        all_added_queue_ids = []

        # add all the above tasks to the queue
        for i, c_task in enumerate(transcription_tasks):

            # generate a unique id if one hasn't been passed
            if queue_id is None:
                next_queue_id = self.processing_queue.generate_queue_id(name=name)

            else:
                # if a unique id was passed, only use it for the first transcription_task
                next_queue_id = queue_id

                # then reset it
                queue_id = None

            if c_task == 'transcribe':
                c_name = 'Transcription of {}'.format(name)

            elif c_task == 'translate':
                c_name = 'Translation of {}'.format(name)

            else:
                c_name = name

            # pass the queue tasks via kwargs
            # we're not adding all the transcription tasks into a single item but going through this loop,
            # because we want to be able to pass different settings for each task,
            # plus we want to see the progress on each queue item separately
            # therefore splitting them into separate queue items makes more sense
            kwargs['tasks'] = [c_task]
            kwargs['task'] = c_task

            # pass the name and queue id
            kwargs['name'] = c_name
            kwargs['queue_id'] = next_queue_id

            # add the audio file path as source file path
            kwargs['source_file_path'] = audio_file_path

            # add the type
            kwargs['item_type'] = 'transcription'

            # send each item to the universal queue
            added_queue_id = self.processing_queue.add_to_queue(**kwargs)

            # add the generated queue id to the list to the queued list
            all_added_queue_ids.append(added_queue_id)

            # if we need to detect speaker changes, add the speaker detection tasks too
            if kwargs.get('transcription_speaker_detection', True):
                kwargs['tasks'] = ['speaker_detection']
                kwargs['name'] = '{} {}'.format(c_name, '(Speaker Detection)')
                kwargs['queue_id'] = None

                kwargs['item_type'] = 'transcription'

                speaker_detection_queue_id = self.processing_queue.add_to_queue(**kwargs)

                # add the generated queue id to the list to the queued list
                all_added_queue_ids.append(speaker_detection_queue_id)

                # add the main transcription queue item as a dependency to the speaker detection queue item
                # this way, when the transcription is done, the speaker detection item will start
                # and retrieve all the data from the transcription queue item
                self.processing_queue.add_dependency(queue_id=speaker_detection_queue_id, dependency_id=next_queue_id)

            # if we need to group questions, add the group questions tasks too
            if kwargs.get('transcription_group_questions', False):

                kwargs['tasks'] = ['group_questions']
                kwargs['name'] = '{} {}'.format(c_name, '(Group Questions)')
                kwargs['queue_id'] = None

                kwargs['item_type'] = 'transcription'

                group_questions_queue_id = self.processing_queue.add_to_queue(**kwargs)

                # add the generated queue id to the list to the queued list
                all_added_queue_ids.append(group_questions_queue_id)

                # add the main transcription queue item as a dependency to the group questions queue item
                # this way, when the transcription is done, the group questions item will start
                # and retrieve all the all the data from the transcription queue item
                self.processing_queue.add_dependency(queue_id=group_questions_queue_id, dependency_id=next_queue_id)

        # return the queue ids
        return all_added_queue_ids

    def transcription_progress(self, queue_id=None, progress=None):
        '''
        Updates the progress of a transcription item in the transcription log
        :param queue_id:
        :param progress:
        :return:
        '''

        # if a progress was passed, update the progress
        if queue_id and progress:
            self.processing_queue.update_queue_item(queue_id=queue_id, save_to_file=False, progress=progress)

        # if no progress was passed, just return the current progress
        elif queue_id:
            return self.processing_queue.get_progress(queue_id=queue_id)

    def add_transcription_file_path_to_queue_item(self, queue_id=None, transcription_file_path=None):
        """
        This function adds the transcription file path to another queue item
        """

        # if no queue id was passed, return False
        if not queue_id:
            return False

        # if no transcription file path was passed, return False
        if not transcription_file_path:
            return False

        # get the queue item
        queue_item = self.processing_queue.get_item(queue_id=queue_id)

        # if no queue item was found, return False
        if not queue_item:
            return False

        # if it has a list of transcription file paths, append the new one
        if queue_item.get('transcription_file_paths', False):

            # append the new transcription file path
            queue_item['transcription_file_paths'].append(transcription_file_path)

        # if it doesn't have a list of transcription file paths, create one
        else:

            # create a list of transcription file paths
            queue_item['transcription_file_paths'] = [transcription_file_path]

        # update the queue item
        self.processing_queue.update_queue_item(**queue_item)

    # TRANSCRIPTION PROCESS METHODS

    def whisper_options(self, **parameters):
        """
        This function looks at all the passed parameters
        and returns the ones that are relevant to mots_whisper
        """

        allowed_parameters = [
            ('audio_file_path', str),
            ('language', str),
            ('model', str),
            ('device', str),
            ('task', str),
            ('initial_prompt', str),

            ('beam_size', int),
            ('best_of', int),
            ('temperature', float),
            ('compression_ratio_threshold', float),
            ('logprob_threshold', float),
            ('no_speech_threshold', float),
            ('word_timestamps', bool),
            ('prepend_punctuations', bool),
            ('append_punctuations', bool),
            ('prompt', str),

            # mots_whisper specific parameters
            ('queue_id', str)
        ]

        filtered_parameters = {}

        # filter out the parameters that are not allowed
        # by checking if they are in the allowed_parameters list
        for param, param_type in allowed_parameters:

            # if the parameter is in the list and is of the correct type
            if param in parameters and isinstance(parameters[param], param_type):

                # add it to the filtered parameters
                filtered_parameters[param] = parameters[param]

        # return only the filtered parameters
        return filtered_parameters

    def get_whisper_available_languages(self) -> list or None:

        available_languages = whisper_tokenizer.LANGUAGES.values()

        if not available_languages or available_languages is None:
            available_languages = []

        return sorted(available_languages)

    def torch_device_type_select(self, device=None):
        '''
        A standardized way of selecting the right Torch device type
        :param device:
        :return:
        '''

        allowed_devices = ['cuda', 'CUDA', 'gpu', 'GPU', 'cpu', 'CPU']

        # change the torch device if it was passed as a parameter
        if device is not None and device in allowed_devices:
            self.torch_device = device

        # if the torch device is set to cuda
        if self.torch_device in ['cuda', 'CUDA', 'gpu', 'GPU']:

            # use CUDA only if available
            if torch.cuda.is_available():
                self.torch_device = device = torch.device('cuda')

            # or let the user know that cuda is not available and switch to cpu
            else:
                logger.warning('CUDA not available. Switching to cpu.')
                self.torch_device = device = torch.device('cpu')

        # if the torch device is set to cpu
        elif self.torch_device in ['cpu', 'CPU']:
            self.torch_device = device = torch.device('cpu')

        # any other setting, defaults to automatic selection
        else:
            # use CUDA if available, or CPU otherwise
            self.torch_device = device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        logger.debug('Using {} for Torch.'.format(device))

        return self.torch_device

    def split_audio_by_intervals(self, audio_array, time_intervals=None, sr=16_000):
        """
        Splits the audio_array according to the time_intervals
        and returns a audio_segments list with multiple audio_arrays
        together with the time_intervals passed to the function
        """

        # reset the audio segments list
        audio_segments = []

        # if there are time segments
        if time_intervals is not None and time_intervals \
                and type(time_intervals) == list and len(time_intervals) > 0:

            # sort the audio segments by start time
            time_intervals = sorted(time_intervals, key=lambda x: x[0])

            # combine overlapping segments
            time_intervals = self.combine_intervals(time_intervals, 0)

            # take each time segment
            for time_interval in time_intervals:
                # calculate duration based on start and end times!!

                # and add it to an audio segments list
                # the format is [start_time, end_time, audio_array]
                audio_segment = [time_interval[0],
                                 time_interval[1],
                                 audio_array[int(time_interval[0] * sr): int(time_interval[1] * sr)]
                                 ]

                audio_segments.append(audio_segment)
            return audio_segments, time_intervals

        # if time_intervals is empty, define it as a single segment,
        # from the beginning to the end (i.e. we're transcribing the full audio)
        time_intervals = [[0, len(audio_array) / sr]]
        audio_segments = [[0, len(audio_array / sr), audio_array]]
        return audio_segments, time_intervals

    def get_speech_intervals(self, audio_segment, **kwargs):
        """
        Returns an array of start and end times of the segments of speech in the audio_segment

        :param audio_segment: a numpy array with the audio segment
        :return: a list of start and end times of the segments of speech in the audio_segment
        """

        sample_rate = kwargs.get('sample_rate', 16_000)

        # Removes silences from the audio file.
        # This results in better transcription quality, without hallucinations.
        try:
            vad_model, utils = torch.hub.load(repo_or_dir='snakers4/silero-vad', model='silero_vad', force_reload=False,
                                              onnx=True, trust_repo=True, verbose=False)
            (get_speech_timestamps, _, read_audio, _, collect_chunks) = utils

        except (PermissionError, FileNotFoundError):
            logger.error(
                'Could not load the VAD model. There might be an issue with your cache folder. ',
                exc_info=True
            )

            logger.error('Try running the tool with Administrator rights '
                         'or try deleting the cache folder mentioned in the error.')

            # pass the error to whatever called this function
            raise

        # convert the audio_segment to a torch tensor
        # if the audio segment is a list containing the start time, end time and the audio array,
        #  we only take the audio array
        if isinstance(audio_segment, list) and len(audio_segment) == 3:
            audio_segment_torch = torch.from_numpy(audio_segment[2])
        else:
            audio_segment_torch = torch.from_numpy(audio_segment)

        speech_timestamps = get_speech_timestamps(audio_segment_torch, vad_model,
                                                  sampling_rate=sample_rate,
                                                  window_size_samples=512,
                                                  speech_pad_ms=kwargs.get('silence_threshold', 200),
                                                  threshold=kwargs.get('silence_threshold', 0.5),
                                                  )

        # convert speech_timestamps to seconds using the sample rate
        # the speech_timestamps format is [{'start': start_time, 'end': end_time], ...]
        speech_timestamps = [[speech_timestamp['start'] / sample_rate, speech_timestamp['end'] / sample_rate]
                             for speech_timestamp in speech_timestamps]

        # if there are no speech_timestamps, return an empty list
        if not speech_timestamps:
            return []

        # combine all the speech_timestamps that are less than X seconds apart
        # this is to avoid having too many small segments
        speech_timestamps = self.combine_intervals(speech_timestamps,
                                                   combine_min_time=kwargs.get('combine_speech_min_time', 3))

        # if the first speech_timestamp starts after 0 but before 1 second
        # we set the start time to 0
        if isinstance(speech_timestamps, list) and 0 < speech_timestamps[0][0] < 1:
            speech_timestamps[0][0] = 0

        return speech_timestamps

    def combine_intervals(self, intervals, combine_min_time):
        """
        Combines intervals that are less than combine_min_time apart
        :param intervals: a list of timestamps
        :param combine_min_time: the minimum time (seconds) between two timestamps to be combined
        :return:
        """

        # if there are no intervals, return an empty list
        if not intervals or not isinstance(intervals, list) or len(intervals) == 0:
            return []

        # sort the timestamps by start time
        intervals = sorted(intervals, key=lambda x: x[0])

        # create a new list to store the combined timestamps
        new_intervals = []

        # take the first timestamp
        current_timestamp = intervals[0]

        # for each timestamp
        for interval in intervals[1:]:
            # if the current timestamp is less than min_time apart from the next timestamp
            if interval[0] - current_timestamp[1] <= combine_min_time:
                # combine the two timestamps
                current_timestamp[1] = interval[1]

            # if the current timestamp is more than min_time apart from the next timestamp
            else:
                # add the current timestamp to the new_timestamps list
                new_intervals.append(current_timestamp)

                # and set the current timestamp to the next timestamp
                current_timestamp = interval

        # add the last timestamp to the new_timestamps list
        new_intervals.append(current_timestamp)

        return new_intervals

    def combine_overlapping_intervals(self, intervals, additional_intervals=None):
        '''
        Given a list of timestamps in the format [[start_time, end_time], ...]
        return a list of timestamps with overlapping timestamps combined
        :param intervals:
        :param additional_intervals:
        :return:
        '''

        # if there are no intervals and no additional intervals, return None
        if (intervals is None or type(intervals) is bool) \
                and additional_intervals is None or type(additional_intervals) is bool:
            return None

        # if there are no intervals but there are additional intervals,
        # return the additional intervals
        if (intervals is None or type(intervals) is bool) \
                and additional_intervals is not None and type(additional_intervals) is not bool:
            return additional_intervals

        # sort the timestamps by start time
        if intervals is not None and type(intervals) is not bool:
            intervals = sorted(intervals, key=lambda x: x[0])

        # if there are additional intervals,
        # get the intersecting intervals
        if additional_intervals is not None and type(additional_intervals) is not bool:

            # sort the additional timestamps by start time
            additional_intervals = sorted(additional_intervals, key=lambda x: x[0])

            intersecting_intervals = []

            # get the intersecting intervals
            for interval in intervals:

                for additional_interval in additional_intervals:

                    if additional_interval[0] <= interval[1] and additional_interval[1] >= interval[0]:
                        intersecting_intervals.append(
                            [max(interval[0], additional_interval[0]), min(interval[1], additional_interval[1])])

            # redeclare the intervals as the intersecting intervals
            intervals = intersecting_intervals

        return intervals

    def pre_process_audio_segment(self, audio_segment, **kwargs):
        """
        Pre processes the audio segment before passing it to the whisper transcribe function
        :param audio_segment:
        :param kwargs:
        :return:
        """

        return audio_segment

    def split_segment_by_word_limits(self, segment, segment_word_limit: int = None,
                                     segment_character_limit: int = None):
        """
        Splits the segment into multiple segments by the given word limit or character limit.
        The word limit will be overridden by the character limit if both are specified.
        """

        if not segment_word_limit and not segment_character_limit:
            return segment

        if not isinstance(segment, dict):
            logger.warning('The segment is either empty or not a dictionary.'
                           'Cannot split segment by word or character limit.')
            return segment

        # if the segment contains no words, we can't perform the split
        # because we don't know the start and end times of the words
        if 'words' not in segment or not segment['words']:
            logger.warning('Segment does not contain words-level timings. '
                           'Cannot split segment by word or character limit.')
            return segment

        # we need to be aware that the segment might be split multiple times
        # so we need to keep track of the current segment index
        current_segment_index = 0

        # we need to keep track of the current segment text - this is what we use to check over and over again
        current_segment_text = segment['text']

        # we need to keep track of the remaining words in the segment
        current_segment_words = segment['words']

        # we need to keep track of the number of words in the segment
        # this might be used to determine the number of words to keep in the first part
        # and the number of words to keep in the second part
        current_segment_words_count = len(current_segment_words)

        # here we keep track of the segments that result after splitting
        resulting_segments = []

        # while the current segment text is longer than the character limit
        # or the current segment has more words than the word limit
        while (segment_character_limit and len(current_segment_text) > segment_character_limit) \
                or (segment_word_limit and current_segment_words_count > segment_word_limit):

            # CHARACTER LIMIT SPLIT
            if segment_character_limit:
                # split the current segment text into two parts
                # the first part is the first segment_character_limit characters
                # the second part is the rest of the characters
                # but make sure that you're not splitting a word
                # so look for the last space before the segment_character_limit
                # and split 1 character before that to include the space and preserve Whisper's formatting
                last_space_index = current_segment_text.rfind(' ', 0, segment_character_limit)

                # if there is no space before the segment_character_limit, try to find the space after the segment_character_limit
                if last_space_index == -1:
                    last_space_index = current_segment_text.find(' ', segment_character_limit)

                # if there is no space before or after the segment_character_limit, don't split the segment
                if last_space_index == -1:
                    break

            # OR WORD LIMIT SPLIT
            else:

                # we preserve the number of words according to the word limit by finding the relevant space index

                # does the segment start with a space?
                # if so, we need to skip the first occurence of the space
                if current_segment_text[0] == ' ':
                    skip = 1

                def find_nth(haystack, needle, n, skip=True):

                    if skip:
                        start = haystack.find(needle, 1)
                    else:
                        start = haystack.find(needle)

                    while start >= 0 and n > 1:
                        start = haystack.find(needle, start + 1)
                        n -= 1
                    return start

                last_space_index = find_nth(current_segment_text, ' ', segment_word_limit)

                # if there is no space after the word limit, don't split the segment
                if last_space_index == -1:
                    break

            # split the segment into two parts
            segment_first_part = current_segment_text[:last_space_index]
            segment_second_part = current_segment_text[last_space_index + 1:]

            # the words list is a list of dictionaries,
            # each dictionary containing the word and its start time, end time and probability
            # considering that the word list is ordered and the words have to have the same order in the resulting segments
            # and also that the words have the exact same length as the text in the segment
            # we can use the length of the first part to determine which words to keep in the first part
            # and which words to keep in the second part

            # keep the character length of the words in the first part
            first_part_words_character_length = 0
            first_part_words = []
            first_part_start = None
            first_part_end = None
            words_to_remove = []
            for word in current_segment_words:

                # if the character length of the words in the first part is longer than the first part
                # then we've reached the end of the first part
                # when calculating the length of the first part,
                # we need to add a space at the beginning to preserve Whisper's formatting
                if first_part_words_character_length + len(word['word']) > len(' ' + segment_first_part):
                    break

                # if the first part start is None, then set it to the current word start time
                if first_part_start is None:
                    first_part_start = word['start']

                first_part_words_character_length += len(word['word'])

                # keep the words in the first part
                first_part_words.append(word)

                # keep updating the first part end time
                first_part_end = word['end']

                # keep track of the words to remove
                # - we can't remove them directly here because we're iterating over the same words list
                words_to_remove.append(word)

            # remove the words that we've kept in the first part
            for word in words_to_remove:
                current_segment_words.remove(word)

            # create a new segment
            if segment_first_part != '':

                # add a space at the beginning to preserve Whisper's formatting
                if segment_first_part[0] != ' ':
                    segment_first_part = ' ' + segment_first_part

                new_segment = {
                    'text': segment_first_part,
                    'start': first_part_start if first_part_start is not None else segment['start'],
                    'end': first_part_end if first_part_end is not None else segment['end'],
                    'words': first_part_words,
                }

                # add the new segment to the resulting segments
                resulting_segments.append(new_segment)

            # set the current segment text to the second part
            current_segment_text = segment_second_part

            # increment the current segment index
            current_segment_index += 1

        # is there anything left in the current segment text?
        if current_segment_text:
            # create a new segment

            # add a space at the beginning to preserve Whisper's formatting
            # the space was most likely removed when splitting the segment
            if current_segment_text[0] != ' ':
                current_segment_text = ' ' + current_segment_text

            new_segment = {
                'text': current_segment_text,
                'start': current_segment_words[0]['start'] if current_segment_words else segment['start'],
                'end': current_segment_words[-1]['end'] if current_segment_words else segment['end'],
                'words': current_segment_words,
            }

            # add the new segment to the resulting segments
            resulting_segments.append(new_segment)

        # return the resulting segments
        return resulting_segments

    def split_segment_on_punctuation_marks(self, segment, punctuation_marks=['.', '!', '?', '…']):
        '''
        Splits a segment on punctuation marks and returns a list of segments, including their start and end times.
        '''

        # if the segment contains no words, we can't perform the split
        # because we don't know the start and end times of the words
        if 'words' not in segment or not segment['words']:
            logger.warning('The segment contains no word-level timings, so we can\'t split it on punctuation marks.')
            return segment

        # the resulting segments
        resulting_segments = []

        # the current segment text
        current_segment_text = segment['text']

        # the current segment words
        current_segment_words = segment['words']

        # convert the punctuation marks to a list if it's a string
        if isinstance(punctuation_marks, str):
            punctuation_marks = list(punctuation_marks)

        # the current segment index
        current_segment_index = 0

        # first, replace all instances of ... with a single …
        current_segment_text = current_segment_text.replace('...', '…')

        # while there are punctuation marks in the current segment text
        while any(punctuation_mark in current_segment_text for punctuation_mark in punctuation_marks):

            # find the first punctuation mark in the current segment text
            first_punctuation_mark_index = min(current_segment_text.find(punctuation_mark)
                                               for punctuation_mark in punctuation_marks if
                                               punctuation_mark in current_segment_text)

            # is the next character a punctuation mark?
            while first_punctuation_mark_index + 1 < len(current_segment_text) and \
                    current_segment_text[first_punctuation_mark_index + 1] in punctuation_marks:
                first_punctuation_mark_index += 1

            # split the segment into two parts, but keep the punctuation mark in the first part
            segment_first_part = current_segment_text[:first_punctuation_mark_index + 1]

            # the words list is a list of dictionaries,
            # each dictionary containing the word and its start time, end time and probability
            # considering that the word list is ordered
            # and the words have are in the same order in the resulting segments,
            # but also that the words have the exact same length as the text in the segment
            # we can use the length of the first part to determine which words to keep in the first part
            # and which words to keep in the second part

            # keep track of the character length of the words in the first part
            first_part_words_character_length = 0
            first_part_words = []

            first_part_start = None
            first_part_end = None
            words_to_remove = []

            # keep track of the start and end times of the first part
            for word in current_segment_words:

                # if the character length of the words in the first part is longer than the first part
                # then we've reached the end of the first part
                # when calculating the length of the first part,
                # we need to add a space at the beginning to preserve Whisper's formatting
                if first_part_words_character_length + len(word['word']) > len(' ' + segment_first_part):
                    break

                # if the first part start is None, it means that we just started working on the first part
                # so set it to the current word start time
                if first_part_start is None:
                    first_part_start = word['start']

                # add the word length, this should also include the space at the beginning,
                # considering that each word contains a space at the beginning when it comes from Whisper
                first_part_words_character_length += len(word['word'])

                # keep the words in the first part
                first_part_words.append(word)

                # keep updating the first part end time
                first_part_end = word['end']

                # keep track of the words to remove
                # - we can't remove them here directly because we're iterating over the same list
                words_to_remove.append(word)

            # remove the words from the current segment words
            for word in words_to_remove:
                current_segment_words.remove(word)

            # create a new segment
            if segment_first_part != '':

                # add a space at the beginning to preserve Whisper's formatting
                if segment_first_part[0] != ' ':
                    segment_first_part = ' ' + segment_first_part

                new_segment = {
                    'text': segment_first_part,
                    'start': first_part_start if first_part_start is not None else segment['start'],
                    'end': first_part_end if first_part_end is not None else segment['end'],
                    'words': first_part_words,
                }

                # add the new segment to the resulting segments
                resulting_segments.append(new_segment)

            # set the current segment text to the second part
            current_segment_text = current_segment_text[first_punctuation_mark_index + 1:]

            # increment the current segment index
            current_segment_index += 1

        # is there anything left in the current segment text?
        if current_segment_text:
            # create a new segment to hold the remaining text

            # add a space at the beginning to preserve Whisper's formatting
            if current_segment_text[0] != ' ':
                current_segment_text = ' ' + current_segment_text

            new_segment = {
                'text': current_segment_text,
                'start': current_segment_words[0]['start'] if current_segment_words else segment['start'],
                'end': current_segment_words[-1]['end'] if current_segment_words else segment['end'],
                'words': current_segment_words,
            }

            # add the segment with the remaining text to the resulting segments
            resulting_segments.append(new_segment)

        # return the resulting segments
        return resulting_segments

    def split_segments(self, segments: list, **kwargs):
        '''
        This splits transcription segments into smaller segments based on:
        - punctuation marks (if the split_on_punctuation_marks option is set)
        - the maximum number of characters per segment (if the max_characters_per_segment option is set)
        - the maximum number of words per segment (if the max_words_per_segment option is set)
        :param segments: the segments to split
        '''

        # if there are no 'words' in the first segment it means that Whisper hasn't returned any word timings
        # in this case, we can't split the segments
        if len(segments) == 0 or 'words' not in segments[0]:
            logger.debug('No word-level timings available, so we can\'t split the segments.')
            return segments

        # get the punctuation mark splitting option
        split_on_punctuation_marks = kwargs.get('split_on_punctuation_marks', False)

        # split the result on punctuation marks if the option is set
        if split_on_punctuation_marks:

            logger.debug('Splitting segments on pre-defined punctuation marks...')

            # get the custom punctuation marks from the config
            custom_punctuation_marks = self.stAI.get_app_setting('transcription_custom_punctuation_marks',
                                                                 default_if_none=['.', '!', '?', '…'])

            # the resulting segments
            new_segments = []

            # take each segment
            for segment in segments:
                # split the segment into multiple segments
                resulting_segment = self.split_segment_on_punctuation_marks(segment,
                                                                            punctuation_marks=custom_punctuation_marks)

                # add the resulting segments to the new segments list
                new_segments.extend(resulting_segment)

            # replace the segments in the result with the new segments
            segments = new_segments

        # get the segment word limit
        segment_word_limit = kwargs.get('max_words_per_segment', None)

        # get the segment character limit
        segment_character_limit = kwargs.get('max_chars_per_segment', None)

        # validate that the segment word limit is an integer
        if segment_word_limit is not None:
            try:
                segment_word_limit = int(segment_word_limit)
            except ValueError:
                segment_word_limit = None

        # validate that the segment character limit is an integer
        if segment_character_limit is not None:
            try:
                segment_character_limit = int(segment_character_limit)
            except ValueError:
                segment_character_limit = None

        # if there is a segment word limit or character limit
        # and the result is longer than the any of the limits
        # (the character limit takes precedence)
        # then split the result into multiple segments
        if segment_character_limit is not None or segment_word_limit is not None:

            logger.debug('Splitting segments on word/character limits...')

            # the resulting segments
            new_segments = []
            # take each segment in the result
            for segment in segments:
                # split the segment into multiple segments
                resulting_segment = self.split_segment_by_word_limits(segment, segment_word_limit,
                                                                      segment_character_limit)

                # add the resulting segments to the new segments list
                new_segments.extend(resulting_segment)

            # replace the segments in the result with the new segments
            segments = new_segments

        return segments

    def post_process_whisper_result(self, audio, result, **kwargs):
        """
        Post processes the result of a whisper transcribe call
        This should be applied for each segment in part to ensure that we're re-merging the entire result correctly
        :param audio:
        :param result:
        :return:
        """

        # don't do anything if there are no segments
        if not isinstance(result, dict) or 'segments' not in result:
            return result

        # split segments if necessary (by punctuation marks or by word/character limits)
        result['segments'] = self.split_segments(result['segments'], **kwargs)

        # get the prevent short gaps option (if any)
        prevent_short_gaps = kwargs.get('prevent_short_gaps', False)

        if prevent_short_gaps:
            try:
                prevent_short_gaps = float(prevent_short_gaps)
            except ValueError:
                prevent_short_gaps = False

        if prevent_short_gaps:
            logger.debug('Filling gaps shorter than {}s in the result...'.format(prevent_short_gaps))

        # do some housekeeping
        # go through each segment in the result
        previous_segment_end_time = None
        new_result_segments = []
        for n, segment in enumerate(result['segments']):

            # do not allow segments that are not dictionaries
            if not isinstance(segment, dict):
                logger.debug('Segment {} is not a dictionary: {}\nRemoving from results.'.format(n, segment))

                # remove the segment from the result
                result['segments'].remove(segment)

                # continue to the next segment
                continue

            # do not allow empty segments or segments that are not strings
            if 'text' not in segment or not isinstance(segment['text'], str) or segment['text'] == '':
                # remove the segment from the result
                result['segments'].remove(segment)

                # continue to the next segment
                continue

            # remove word timestamps from the result to avoid confusion,
            # until the word-based transcript editing is implemented
            if 'words' in segment and kwargs.get('post_remove_word_timestamps', False):
                del segment['words']

            # add the segment to the new result segments
            new_result_segments.append(segment)

            # if we're supposed to prevent short gaps between segments
            # and the previous segment ended less than 'prevent_short_gaps' seconds ago
            # do this
            if previous_segment_end_time is not None and isinstance(prevent_short_gaps, float):

                # get the time difference between the end of the previous segment and the start of this segment
                time_difference = segment['start'] - previous_segment_end_time

                # if the time difference is less than the prevent_short_gaps value
                if time_difference < prevent_short_gaps:
                    # set the end time of the previous segment to the start time of this segment
                    new_result_segments[-2]['end'] = segment['start']

            # if we made it so far it means that the segment will be included in the result,
            # so remember the end time of this segment
            previous_segment_end_time = segment['end']

        # replace the segments in the result with the new segments
        result['segments'] = new_result_segments

        return result

    def speaker_detection(self, transcription_file_path, **kwargs):

        # get the transcription object
        transcription = Transcription(transcription_file_path=transcription_file_path)

        if not transcription:
            fail_error = 'Unable to detect speakers - no transcription available: {}.'.format(transcription_file_path)
            logger.error(fail_error)

            if kwargs.get('queue_id'):
                self.processing_queue.update_status(kwargs['queue_id'], status='failed', fail_error=fail_error)

            return None

        # only check non-meta segments
        # the group time intervals may contain the meta segments too depending on their start time,
        # but at least we're not processing them
        # segments = TranscriptionUtils.filter_segments(transcription.segments_dict, filter_meta=True)

        segments = transcription.segments_dict

        # if the transcription already has speaker segments that start with 'Speaker ',
        # take the last speaker id and increment it by 1
        # otherwise, start from 1
        speaker_id_offset = 0

        # update the progress in the queue
        queue_id = kwargs.get('queue_id', None)
        self.processing_queue.update_queue_item(
            queue_id=queue_id, save_to_file=False, status='preparing segments'
        )

        # get the last speaker id from the transcription
        for segment in segments:

            # only look at the speaker segments
            if 'category' in segment and segment['category'] == 'speaker'  \
                    and 'text' in segment and segment['text'].startswith('Speaker '):
                speaker_id_offset = max(speaker_id_offset, int(segment['text'].split(' ')[1]))

        threshold = kwargs.get('transcription_speaker_detection_threshold', None)

        # update the progress in the queue
        queue_id = kwargs.get('queue_id', None)
        self.processing_queue.update_queue_item(
            queue_id=queue_id, save_to_file=False, status='loading'
        )

        processed_segments = 0
        resulting_segments = []
        for resulting_segments, speaker_embeddings in detect_speaker_changes(
            segments=segments, audio_file_path=transcription.audio_file_path, threshold=threshold,
            device_name=kwargs.get('device', None),
            time_intervals=kwargs.get('time_intervals', None),
            speaker_id_offset=speaker_id_offset,
            step_by_step=True
        ):
            processed_segments += 1

            if queue_id is None:
                continue

            # calculate the progress
            progress = min(100, int((processed_segments / len(segments)) * 100))

            # update the progress in the queue
            self.processing_queue.update_queue_item(
                queue_id=queue_id, save_to_file=False, progress=progress, status='detecting changes'
            )

            # cancel detection if user requested it
            if self.processing_queue.cancel_if_canceled(queue_id=queue_id):

                # and return none
                return None

        # no time intervals were passed or if the time intervals is not a list of lists
        if not kwargs.get('time_intervals', None) or not isinstance(kwargs['time_intervals'], list):

            # set the time intervals to the start and end times of the first and last segments
            kwargs['time_intervals'] = [[segments[0]['start'], segments[-1]['end']]]

        # now take all the resulting segments and turn the speaker id's into meta segments
        last_speaker_id = None
        speaker_segments = []
        for segment in resulting_segments:

            # if there's no speaker_id for the segment, skip
            if 'speaker_id' not in segment:
                continue

            # if the speaker id is not the same as the last speaker id
            if not last_speaker_id or segment['speaker_id'] != last_speaker_id:

                # create a new meta segment
                meta_segment = {
                    'start': segment['start'],
                    'end': segment['start'],
                    'meta': True,
                    'category': 'speaker',
                    'text': 'Speaker {}'.format(segment['speaker_id'])
                }

                # add the meta segment to the speaker_segments
                speaker_segments.append(meta_segment)

            # update the last speaker id
            last_speaker_id = segment['speaker_id']

        if not speaker_segments:
            logger.debug('No speakers detected for the selected time intervals: {}.'.format(kwargs['time_intervals']))
            return None

        # remove the speaker segments between the passed time intervals
        for time_interval in kwargs['time_intervals']:
            transcription.delete_segments_between(
                start=time_interval[0], end=time_interval[1],
                additional_condition=lambda l_segment: (l_segment.meta and l_segment.category == 'speaker')
            )

        # add the new segments to the transcription
        transcription.add_segments(speaker_segments)

        # save the transcription
        transcription.save_soon(sec=0)

        # if we have a queue_id, update the status to done
        if kwargs.get('queue_id', None):
            self.processing_queue.update_status(queue_id=kwargs.get('queue_id', None), status='done')

        # update all the observers that are listening for this transcription
        self.notify_observers('update_transcription_{}'.format(transcription.transcription_path_id))

        return speaker_segments

    def whisper_transcribe_segments(self, audio_segments, task, other_options, queue_id=None):
        """
        Transcribes only the passed audio segments
        and offsets the transcription segments start and end times

        Only returns the transcription segments
        """

        # get the transcription object if a transcription_file_path exists
        transcription = Transcription(transcription_file_path=other_options.get('transcription_file_path')) \
            if other_options.get('transcription_file_path', None) else None

        # if the transcription object doesn't exist,
        # this function will simply return the results from the transcribe function
        # otherwise, it will also update the transcription object with the results and save it

        results = {'segments': []}
        result = None

        # this will be counted up for each segment to provide a unique id
        id_count = 0

        # the total duration of the audio is the sum of the durations of each audio segment
        # the duration of each audio segment is the end time minus the start time
        total_duration = sum([audio_segment[1] - audio_segment[0] for audio_segment in audio_segments])

        # transcribe each audio segment
        previous_progress = 0
        next_segment_id = 0 if not transcription else transcription.generate_new_segment_id()
        for audio_segment in audio_segments:

            if len(audio_segment) != 3:
                logger.warning('Audio segment must be a list of [start, end, audio]')
                continue

            # the start and end times of the audio segment which we will use to offset the results below
            audio_segment_start = audio_segment[0]
            audio_segment_end = audio_segment[1]

            # if we have a transcription object, first delete any existing segments from this time interval
            if transcription is not None:
                transcription.delete_segments_between(start=audio_segment_start, end=audio_segment_end)

            # pre process the audio segment
            audio_segment = self.pre_process_audio_segment(audio_segment, **other_options)

            # only send to whisper the options that it knows
            decoding_options = self.whisper_options(**other_options.get('whisper_options', {}))

            # do not send an empty string as the language
            if 'language' in decoding_options and (
                    not isinstance(decoding_options['language'], str)
                    or decoding_options['language'] is None
                    or decoding_options['language'] == ''
            ):
                del decoding_options['language']

            # run whisper transcribe on the audio segment
            result = self.whisper_model.transcribe(audio_segment[2],
                                                   task=task,
                                                   verbose=True,
                                                   queue_id=queue_id,
                                                   toolkit_ops_obj=self,
                                                   total_duration=total_duration,
                                                   audio_segment_duration=audio_segment[1] - audio_segment[0],
                                                   previous_progress=previous_progress,
                                                   **decoding_options
                                                   )

            # remove word timestamps from final transcription until we implement word-based editing
            other_options['post_remove_word_timestamps'] = \
                other_options.get('post_remove_word_timestamps', True)

            # post process the result for this audio segment
            result = self.post_process_whisper_result(audio_segment[2], result, **other_options)

            # get the progress of the transcription so far,
            # so we can pass it to the next audio segment for the progress calculation
            previous_progress = self.transcription_progress(queue_id)

            # now process the result and add the original start time offset
            # to each transcript segment start and end times

            # if there are segments in the result
            # re-calibrate the start and end times of each segment according to the offset
            # and add them to the transcription
            if isinstance(result, dict) and 'segments' in result and result['segments']:

                current_segment_batch = []

                # take each segment and add the offset to the start and end time
                for i, transcript_segment in enumerate(result['segments']):

                    # remove tokens, seek, temperature, avg_logprob, compression_ratio and no_speech_prob
                    # unless otherwise specified
                    if not other_options.get('keep_whisper_debug_info', False):
                        for key in ['tokens', 'seek', 'temperature', 'avg_logprob', 'compression_ratio',
                                    'no_speech_prob']:
                            if key in transcript_segment:
                                del transcript_segment[key]

                    # add the offset to the start and end time
                    transcript_segment['start'] += audio_segment_start
                    transcript_segment['end'] += audio_segment_start

                    # avoid end time being larger than the interval end time
                    if transcript_segment['end'] > audio_segment_end:
                        transcript_segment['end'] = audio_segment_end

                    # also avoid start time being smaller than the interval start time
                    if transcript_segment['start'] < audio_segment_start:
                        transcript_segment['start'] = audio_segment_start

                    # if the segment contains a 'words' key,
                    # then add the offset to the start and end time of each word
                    if 'words' in transcript_segment:
                        for word in transcript_segment['words']:
                            word['start'] += audio_segment_start
                            word['end'] += audio_segment_start

                            # avoid end time being larger than the interval end time
                            if word['end'] > audio_segment_end:
                                word['end'] = audio_segment_end

                            # also avoid start time being smaller than the interval start time
                            if word['start'] < audio_segment_start:
                                word['start'] = audio_segment_start

                    transcript_segment['id'] = next_segment_id + id_count
                    id_count += 1

                    # add the transcription of the audio segment to the results list
                    results['segments'].append(transcript_segment)

                    # add the segment to the current batch
                    current_segment_batch.append(transcript_segment)

                    # add the language to the result
                    results['whisper_language'] = result['language'] if 'language' in result else ''

                # add the segment to the transcription object (if any)
                #  because it makes the Transcription object re-set all the segments each time
                #  we need to have a bulk add
                if transcription is not None:
                    transcription.add_segments(current_segment_batch)

            # save the transcription for each audio segment
            if transcription is not None:
                transcription.save_soon()

        # copy the status from the result to the results (if any)
        # normally we should only get a status if the transcription was canceled or it failed
        if isinstance(result, dict) and 'status' in result:
            results['status'] = result['status']

        return results

    def exclude_segments_by_intervals(self, audio_array, time_intervals, excluded_time_intervals, sr):
        """
        Excludes certain audio segments from audio_array according to the excluded_time_intervals
        and returns a new audio_array with the excluded segments removed
        """

        # if there are no excluded time intervals, return the original audio array
        if not (excluded_time_intervals and time_intervals):
            audio_segments, new_time_intervals = self.split_audio_by_intervals(audio_array, time_intervals, sr)
            return audio_segments, new_time_intervals

        # sort the time intervals by start time
        excluded_time_intervals.sort(key=lambda x: x[0])

        # use this to keep track of the new time intervals
        new_time_intervals = []

        # for each time interval, check if it overlaps with any of the excluded time intervals
        for interval in time_intervals:
            temp_intervals = [interval]

            # for each excluded time interval, split the temp_intervals
            for excluded_interval in excluded_time_intervals:
                new_temp_intervals = []

                # for each temp_interval, check if it overlaps with the excluded time interval
                for temp_interval in temp_intervals:

                    # if the excluded interval is completely before or after the temp interval, do nothing
                    if excluded_interval[1] <= temp_interval[0] or excluded_interval[0] >= temp_interval[1]:
                        new_temp_intervals.append(temp_interval)

                    # if the excluded interval is completely inside the temp interval, split the temp interval
                    else:
                        # if the start of the excluded interval is inside the temp interval
                        if temp_interval[0] < excluded_interval[0]:
                            new_temp_intervals.append([temp_interval[0], excluded_interval[0]])

                        # if the end of the excluded interval is inside the temp interval
                        if temp_interval[1] > excluded_interval[1]:
                            new_temp_intervals.append([excluded_interval[1], temp_interval[1]])

                # replace the temp intervals with the new temp intervals
                temp_intervals = new_temp_intervals

            # add the temp intervals to the new time intervals
            new_time_intervals.extend(temp_intervals)

        # split the audio array by the new time intervals
        audio_segments, new_time_intervals = self.split_audio_by_intervals(audio_array, new_time_intervals, sr)

        return audio_segments, new_time_intervals

    def _initialize_whisper_transcribe(self, queue_id=None, **other_options):
        """
        This initializes everything that is needed for whisper
        """

        torch_device_changed = False
        # change the torch device if it was passed and it's different from the current one
        if other_options.get('device', None) and self.torch_device != other_options.get('device'):
            # select the new whisper device but take it through the torch device selection to make sure it's valid
            self.torch_device = self.torch_device_type_select(other_options.get('device', None))

            torch_device_changed = True

        # load OpenAI Whisper model
        # if it wasn't loaded before, if the model name changed (via other_options) or if the torch device changed
        if self.whisper_model is None \
                or ('model_name' in other_options and self.whisper_model_name != other_options['model_name']) \
                or torch_device_changed:

            # use the model name that was passed in the call or the one that's already set
            self.whisper_model_name = other_options.get('model_name', self.whisper_model_name)

            # update the status of the item in the queue
            self.processing_queue.update_queue_item(queue_id=queue_id, status='loading {} model'
                                                    .format(self.whisper_model_name))

            # cancel transcription if user requested it
            if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
                return None

            # if the Whisper transformer model was never downloaded, log that we're downloading it
            model_downloaded_before = True
            if self.stAI.get_app_setting(setting_name='whisper_model_downloaded_{}'
                    .format(self.whisper_model_name),
                                         default_if_none=False
                                         ) is False:
                logger.warning('The whisper {} model may need to be downloaded and could take a while '
                               'depending on the Internet connection speed. '
                               .format(self.whisper_model_name)
                               )

                # update the status of the item in the transcription log
                self.processing_queue.update_queue_item(queue_id=queue_id, status='downloading model')

                model_downloaded_before = False

            # cancel transcription if user requested it
            if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
                return None

            logger.info('Loading Whisper {} model.'.format(self.whisper_model_name))
            try:
                self.whisper_model = whisper.load_model(self.whisper_model_name, device=self.torch_device)
            except Exception as e:
                fail_error = 'Error loading Whisper {} model: {}'.format(self.whisper_model_name, e)
                logger.error(fail_error)

                # update the status of the item in the transcription log
                self.processing_queue.update_queue_item(queue_id=queue_id, status='failed', fail_error=fail_error)

            # once the model has been loaded, we can note that in the app settings
            # this is a wat to keep track if the model has been downloaded or not
            # but it's not 100% reliable and we may need to find a better way to do this in the future
            if not model_downloaded_before:
                self.stAI.save_config(setting_name='whisper_model_downloaded_{}'.format(self.whisper_model_name),
                                      setting_value=True)

            # let the user know if the whisper model is multilingual or english-only
            logger.info('Selected Whisper model "{}" is {}.'.format(
                str(self.whisper_model_name),
                'multilingual' if self.whisper_model.is_multilingual else 'English-only'
            ))

        # cancel transcription if user requested it
        if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
            return None

        return True

    def _split_audio_into_segments(self, audio_file_path, queue_id=None, **kwargs):
        """
        This splits the audio into segments that are suitable for Whisper
        It also takes into consideration any inclusion or exclusion intervals
        """

        # load audio file as array using librosa
        # this should work for most audio formats
        try:
            audio_array, sr = librosa.load(audio_file_path, sr=16_000)

        # if the above fails, try this:
        except:

            logger.warning('Falling back to 48000Hz for {} due to audio format, '
                           'but this might provide inaccurate results. '
                           'Please use a recommended file format to avoid falling back to this default.'
                           .format(os.path.basename(audio_file_path)))

            raw_sr = 48000
            sr = 16000

            # if this is a video file, extract the audio from it
            try:
                video = VideoFileClip(audio_file_path)
                raw_audio_array = video.audio.to_soundarray(fps=raw_sr)
            except:
                # last chance, if this is audio-only, try to load it with AudioFileClip

                audio = AudioFileClip(audio_file_path)
                raw_audio_array = audio.to_soundarray(fps=raw_sr)

            audio_array = librosa.core.resample(np.asfortranarray(raw_audio_array.T), orig_sr=raw_sr, target_sr=sr)
            audio_array = librosa.core.to_mono(audio_array)

            # change to float32
            audio_array = np.asarray(audio_array, dtype=np.float32)

        # cancel transcription if user requested it
        if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
            return None, None

        # TIME INTERVALS PRE-PROCESSING starts here

        # assume no time intervals
        time_intervals = None

        # if pre_detect_speech is True, detect speech intervals in the audio
        if kwargs.get('pre_detect_speech', None):

            # update the status of the item in the transcription log
            self.processing_queue.update_queue_item(queue_id=queue_id, status='pre-detecting speech')

            logger.info('Pre-detecting speech intervals in {}.'.format(kwargs.get('name', 'audio file')))

            # perform speech detection
            time_intervals = self.get_speech_intervals(audio_array)

            # fail if no speech was detected
            if len(time_intervals) == 0:
                fail_error = 'No speech was detected in {}.'.format(kwargs.get('name', 'audio file'))
                logger.info(fail_error)

                # update the queue item status
                if queue_id is not None:
                    self.processing_queue.update_queue_item(queue_id=queue_id, status='failed', fail_error=fail_error)

                return None, None

        # if time_intervals was passed from the request, take them into consideration
        # but only if they are not boolean (True or False)
        if kwargs.get('time_intervals', None) \
                and type(kwargs.get('time_intervals', None)) is not bool:

            # if no time intervals were set before, use the ones from the request
            if time_intervals is None:
                time_intervals = kwargs['time_intervals']
            else:
                # intersect the time intervals from the request
                # with the previously had time intervals (from speech for eg.)
                time_intervals = \
                    self.combine_overlapping_intervals(kwargs.get('time_intervals'), time_intervals)

        # split the audio into segments according to the time intervals
        # in case no time intervals were passed, this will just return one audio segment with the whole audio
        audio_segments, time_intervals = self.split_audio_by_intervals(audio_array, time_intervals, sr)

        # cancel transcription if user requested it
        if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
            return None, None

        # exclude time intervals that need to be excluded
        if kwargs.get('excluded_time_intervals', None) \
                and type(kwargs.get('excluded_time_intervals', None)) is not bool:

            audio_segments, time_intervals = self.exclude_segments_by_intervals(
                audio_array, time_intervals, kwargs.get('excluded_time_intervals'), sr=sr
            )

        # last chance to cancel if user requested it
        # before the transcription process starts
        if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
            return None, None

        if len(audio_segments) > 1:
            logger.debug('Split audio {} into {} segments: {}'.format(
                kwargs.get('name', 'from file'),
                len(audio_segments),
                ', '.join([str(float(x[0]))+'-'+str(float(x[1])) for x in time_intervals])))

        return audio_segments, time_intervals

    def whisper_transcribe(self, name: str = None, audio_file_path: str = None, task=None,
                           target_dir=None, queue_id=None, return_path=False, **other_options) -> bool or str:
        """
        This prepares and transcribes audio using Whisper
        :param name:
        :param audio_file_path:
        :param task:
        :param target_dir:
        :param queue_id:
        :param return_path: if True, returns the path to the transcription file if successful
        :param other_options:
        :return:
        """

        # if no audio file path was passed, try to use the source file path if it was passed
        audio_file_path = audio_file_path or other_options.get('source_file_path')

        # don't continue if we don't have an audio file path
        if audio_file_path is None or not audio_file_path:
            fail_error = 'No audio file path was passed to whisper_transcribe. Aborting.'
            logger.warning(fail_error)

            # update the queue item status
            if queue_id is not None:
                self.processing_queue.update_queue_item(queue_id=queue_id, status='failed', fail_error=fail_error)

            return False

        # use the directory where the file is stored if another one wasn't passed
        target_dir = target_dir if target_dir and os.path.isdir(target_dir) else os.path.dirname(audio_file_path)

        # the transcription_file_path is either something that was sent via other_options
        # or it's the audio_file_path, but with the extension changed to .transcription.json
        transcription_file_path = other_options.get('transcription_file_path', None) \
            or os.path.join(target_dir, '{}.transcription.json'.format(os.path.basename(audio_file_path)))

        # if we're currently not retranscribing or not supposed to overwrite an existing transcription file
        if os.path.exists(transcription_file_path) \
                and other_options.get('overwrite', False) is False \
                and other_options.get('retranscribe', False) is False:

            # use the the next available transcription file path
            transcription_file_path = TranscriptionUtils.add_count_to_transcription_path(transcription_file_path)

        # let's instantiate the transcription object
        transcription = Transcription(transcription_file_path=transcription_file_path)

        # no matter if a path was sent or not, let's set it here
        other_options['transcription_file_path'] = transcription_file_path

        # use the name of the file in case the name wasn't passed
        other_options['name'] \
            = name = name or os.path.basename(audio_file_path)

        # set a few things in the transcription object
        transcription.set('audio_file_path', os.path.basename(audio_file_path))
        transcription.set('name', name)
        transcription.set('transcription_id', transcription.generate_id())

        # this marks the transcription as incomplete so that it could be resumed later
        transcription.set('incomplete', True)

        # set some more things in the transcription object
        transcription.set('task', task)
        transcription.set('whisper_model', self.whisper_model_name)

        # initialize whisper and get the audio array and sample rate
        if not self._initialize_whisper_transcribe(queue_id=queue_id, **other_options):
            return None

        # split the audio into segments according to the time intervals and pre-detect speech if requested
        audio_segments, time_intervals = self._split_audio_into_segments(
            audio_file_path=audio_file_path, queue_id=queue_id, **other_options)

        if not audio_segments:
            return None

        # update the correct status depending if this is a retranscribe operation or not
        # and if the transcription file already exists
        if transcription.exists and other_options.get('retranscribe', False):
            self.processing_queue.update_queue_item(queue_id=queue_id, status='re-transcribing')
        else:
            self.processing_queue.update_queue_item(queue_id=queue_id, status='transcribing')

        # technically the transcription process starts here, so start a timer for statistics
        transcription_start_time = time.time()

        # let the user know the transcription process has started
        if isinstance(time_intervals, list):
            time_intervals_str = ", ".join([f"{start}-{end}" for start, end in time_intervals])
            debug_message = "Transcribing {} between: {}.".format(name, time_intervals_str)
        else:
            debug_message = "Transcribing {}.".format(name)
        # logger.info(debug_message)

        if self.toolkit_UI_obj:
            self.toolkit_UI_obj.notify_via_os("Starting Transcription",
                                              text="Transcribing {}".format(name),
                                              debug_message=debug_message)

        # initialize empty result
        result = None

        # transcribe the audio segments
        # (or just one audio segment with the whole audio if no time intervals were passed)
        try:
            result = self.whisper_transcribe_segments(audio_segments=audio_segments,
                                                      task=task,
                                                      other_options=other_options,
                                                      queue_id=queue_id
                                                      )
        except Exception as e:
            import traceback
            exc_info = traceback.format_exc()

            # format your message
            fail_error = ('Error transcribing audio {} using Whisper.\n'
                          '{}\n {}').format(name, e, exc_info)

            # log the message
            logger.error(fail_error)

            # update the status of the item in the transcription log
            self.processing_queue.update_queue_item(
                queue_id=queue_id, status='failed', progress='', fail_error=fail_error)

        # was the transcription canceled or failed?
        # if whisper returned None or a dict with status failed or canceled
        if result is None \
                or (type(result) is dict and 'status' in result
                    and (result['status'] == 'failed' or result['status'] == 'canceled')
                    ):
            # copy the status from the result to the log status
            # or simply set it to failed if the result is None
            queue_status = result['status'] if type(result) is dict and 'status' in result else 'failed'

            # set the fail error message
            fail_error = 'See app.log for possible details.' if queue_status == 'failed' else None

            self.processing_queue.update_queue_item(
                queue_id=queue_id, status=queue_status, progress='', fail_error=fail_error)

            return None

        # let the user know that the speech was processed
        notification_msg = "Finished transcription for {} in {} seconds" \
            .format(name, round(time.time() - transcription_start_time))

        if self.toolkit_UI_obj:
            self.toolkit_UI_obj.notify_via_os("Finished Transcription", notification_msg, notification_msg)
        else:
            logger.info(notification_msg)

        # update the status of the item in the transcription log
        self.processing_queue.update_queue_item(queue_id=queue_id, status='saving files', progress='')

        # if we made it here, it means that the transcription is complete
        transcription.set('incomplete', False)

        # save the transcription file once here
        transcription.save_soon(sec=0)

        # take care of the metadata and project related stuff
        self.process_transcription_metadata(other_options=other_options, transcription=transcription)

        # save one more time and process the auxiliaries if needed
        auxiliaries = other_options.get(
            'transcription_auxiliaries',
            self.stAI.get_app_setting('transcription_auto_save_aux', default_if_none=None)
        )

        if auxiliaries:
            transcription.save_soon(sec=0, force=True, auxiliaries=auxiliaries)

        # delete the render.json file if it exists
        if other_options.get('source_file_path', None) and other_options.get('ingest_delete_render_info_file', False):

            source_file_path = other_options.get('source_file_path', None)
            render_json_file_path = "{}.json".format(source_file_path)

            # delete the render.json file if it exists
            TranscriptionUtils.delete_render_json(render_json_file_path=render_json_file_path)

        # cancel transcription if user requested it
        if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
            return None

        # get the queue item again, in case it was updated while we were transcribing
        queue_item = self.processing_queue.get_item(queue_id=queue_id)
        video_indexing_queue_ids = queue_item.get('video_indexing_queue_ids', False)

        # if a video_indexing_queue_id is in this item's queue item,
        # add the transcription file path to the video indexing queue item
        # this is so that the video indexing function adds its own resulting file paths to this transcription
        if video_indexing_queue_ids and isinstance(video_indexing_queue_ids, list):

            for video_indexing_queue_id in video_indexing_queue_ids:
                self.add_transcription_file_path_to_queue_item(
                    queue_id=video_indexing_queue_id,
                    transcription_file_path=transcription.transcription_file_path)

        # when done, change the status in the queue, clear the progress
        # and also add the transcription file path to the queue item
        self.processing_queue.update_queue_item(
            queue_id=queue_id,
            status='done',
            progress='',
            transcription_file_path=transcription.transcription_file_path
        )

        return True if not return_path else transcription.transcription_file_path

    def process_transcription_metadata(self, other_options, transcription: Transcription | str):
        """
        We use this usually at the end of whisper_transcribe() and index_video()
        to set any metadata or project related stuff.
        This also deletes the render.json file if requested.
        """

        # if the transcription is none, return
        if transcription is None:
            return

        # if the transcription is a string, then it's a file path
        if isinstance(transcription, str):
            transcription = Transcription(transcription_file_path=transcription)

        # if we're not re-transcribing or not supposed to overwrite an existing transcription file
        if not other_options.get('retranscribe', False):

            # if a timeline_name was sent, remember it for later
            timeline_name = other_options.get('timeline_name', None)

            # add the timeline name to the transcription data, if there is one
            if timeline_name:
                transcription.set('timeline_name', timeline_name)

            # if a project_name was sent, remember it for later
            project_name = other_options.get('project_name', None)

            # add the project name to the transcription data, if there is one
            if project_name is not None:
                transcription.set('project_name', project_name)

        # update other stuff, like metadata and project and timeline names
        # but only use them if they're not empty
        if 'timeline_fps' in other_options and other_options['timeline_fps']:
            transcription.set('timeline_fps', other_options['timeline_fps'])

        if 'timeline_start_tc' in other_options and other_options['timeline_start_tc']:
            transcription.set('timeline_start_tc', other_options['timeline_start_tc'])

        # if there's a project_name and a timeline_name in the transcription
        # link the transcription to the project and timeline
        if isinstance(transcription, Transcription) \
                and transcription.project_name is not None and transcription.timeline_name is not None:
            project = Project(project_name=transcription.project_name)

            project.link_transcription_to_timeline(
                transcription_file_path=transcription.transcription_file_path,
                timeline_name=transcription.timeline_name
            )

            # notify the observers that the project has changed
            self.notify_observers('project_changed')

        # if a timeline_name wasn't set, but a project_name was set,
        # just link the transcription to the project
        elif isinstance(transcription, Transcription) \
                and transcription.project_name is not None and transcription.timeline_name is None:
            project = Project(project_name=transcription.project_name)

            project.link_to_project(object_type='transcription', file_path=transcription.transcription_file_path)

            # notify the observers that the project has changed
            self.notify_observers('project_changed')

        # save the transcription to file with all the added data
        transcription.save_soon(sec=0)

    # SEARCH/CLASSIFICATION PROCESS METHODS

    def classify_segments(self, segments: list, labels: list,
                          min_confidence: int or list = 0.55, multi_label_pass: list = None, **kwargs):
        """
        Classifies segments into different types using the transformers zero-shot-classification pipeline
        :param segments: the segments to classify
        :param labels: the labels to use for classification, if a list of lists is provided,
                        a multi-label classification is performed, taking into consideration each group of labels
        :param min_confidence: the minimum confidence for a classification to be considered valid,
                                if a list is provided, then the confidence is calculated for multi_label_pass label group
        :param multi_label_pass: a list of groups of labels that need to be passed together,
                                so that the segment stays in the result
        """

        if segments is None or len(segments) == 0:
            logger.debug('No segments to classify.')
            return None

        # make sure that if a list of lists of labels is provided,
        # and also a list of minimum confidence values is provided,
        # then the number of confidence values matches the number of label groups
        # also don't allow a single label group and a list of confidence values
        if (isinstance(labels[0], list) and isinstance(min_confidence, list) and len(labels) != len(min_confidence)
                or (isinstance(labels[0], str) and isinstance(min_confidence, list))
        ):
            logger.error("The number of label groups doesn't match the number of minimum confidence values.")
            raise Exception("The number of label groups doesn't match the number of minimum confidence values.")

        if isinstance(labels[0], list) and len(labels) < len(multi_label_pass):
            logger.warn("The number of label groups is less than the number of multi-label-pass groups. "
                        "Disabling multi-label-pass.")
            multi_label_pass = None

        # using tensorflow but with another model,
        # because facebook/bart-large-mnli is not available in tensorflow
        # from transformers import TFAutoModelForSequenceClassification, AutoTokenizer
        # tokenizer = AutoTokenizer.from_pretrained('roberta-large-mnli')
        # model = TFAutoModelForSequenceClassification.from_pretrained('roberta-large-mnli')
        # classifier = pipeline('zero-shot-classification', model=model, tokenizer=tokenizer)

        # start classification process
        model_name = self.stAI.get_app_setting('text_classifier_model', default_if_none='facebook/bart-large-mnli')

        logger.debug('Loading text classifier model: {}'.format(model_name))

        # get the zero-shot-classification pipeline
        # if this is an arm64 mac use mps as device
        if is_arm64_mac():
            classifier = pipeline('zero-shot-classification', model=model_name, device='mps',)
        elif torch.cuda.is_available():
            logger.debug('Using GPU for classification.')
            classifier = pipeline('zero-shot-classification', model=model_name, device='cuda')
        else:
            classifier = pipeline('zero-shot-classification', model=model_name)

        logger.debug('Classifying segments using the following labels: {}'.format(labels))

        # go through each segment and classify it
        classified_segments = {}

        # use tqdm to show a progress bar while classifying segments
        with tqdm.tqdm(segments, desc='Classifying segments', total=len(segments)) as pbar:

            for i, segment in enumerate(segments):

                # if this is a transcription segment, get the text and words,
                # or assume it's a dict and get them from there
                segment_text = segment.text \
                    if isinstance(segment, TranscriptionSegment) else segment.get('text', None)
                segment_words = segment.words \
                    if isinstance(segment, TranscriptionSegment) else segment.get('words', None)

                # skip segments that don't have any text or words
                if not segment_text and not segment_words:
                    logger.debug("Skipping segment classification because it doesn't have any text or words: {}"
                                 .format(segment))
                    continue

                # if the text is empty, try to get the text from the words
                if not segment_text or segment_text.strip() == '':
                    segment_text = ' '.join([word['word'] for word in segment_words])

                # if the text is still empty, skip the segment
                if not segment_text or segment_text.strip() == '':
                    logger.debug("Skipping segment classification because it doesn't have any text: {}"
                                 .format(segment))
                    continue

                # classify the segment

                # if labels is a list of strings, do a normal classification
                if isinstance(labels, list) and isinstance(labels[0], str):
                    classification = classifier(segment_text, labels)

                    # if the classification confidence is too low, skip the segment
                    if min_confidence and classification['scores'][0] < min_confidence:
                        logger.debug('Skipping segment classification because the confidence is too low {}: {}'
                                     .format(classification['scores'][0], segment))
                        continue

                    # add it to the corresponding list, but first make sure the label exists
                    if classification['labels'][0] not in classified_segments:
                        classified_segments[classification['labels'][0]] = []

                    classified_segments[classification['labels'][0]].append(segment)

                    # clear classification to free up memory
                    del classification

                # if labels is a list of lists, do a multi-label classification
                elif isinstance(labels, list) and isinstance(labels[0], list):

                    # reset the current_segment_passed_classification to True,
                    # until we find a label that doesn't pass
                    current_segment_passed_classification = True

                    # take each label groups, one by one and use them to classify the segment
                    for sub_labels in labels:

                        # if the segment didn't pass the classification for the previous label check, skip it
                        if not current_segment_passed_classification:
                            continue

                        # classify the segment using this label
                        classification = classifier(segment_text, sub_labels)

                        # if the min_confidence is a list, use the index of the current label group
                        # to get the corresponding min_confidence value
                        if isinstance(min_confidence, list):
                            min_confidence = min_confidence[labels.index(sub_labels)]

                        # for a multi-label classification,
                        # we need to check if the confidence is high enough for each label
                        # so if it isn't, we skip the segment - which means that all other remaining labels
                        # will be skipped as well
                        if classification['scores'][0] < min_confidence:
                            current_segment_passed_classification = False
                            logger.debug('Skipping segment classification for the following segment '
                                         'because a confidence of {}'
                                         'is too low to classify it in any of the labels {}: \n{}\n\n'
                                         .format(classification['scores'][0], sub_labels, segment_text))
                            continue

                        # add it to the corresponding list, but first make sure the label exists
                        if classification['labels'][0] not in classified_segments:
                            classified_segments[classification['labels'][0]] = []

                        classified_segments[classification['labels'][0]].append(segment)

                        # clear classification to free up memory
                        del classification

                else:
                    logger.error('Invalid labels for classification: {}'.format(labels))
                    continue

                # update progress bar - this should be done after each segment is classified
                # because the progress bar is based on the number of segments
                pbar.update(1)

                # get the percentage of progress
                progress = int(((i + 1) / len(segments)) * 100)

                # if there's a queue_id, update the queue item with the progress and some output
                if kwargs.get('queue_id'):
                    self.processing_queue.update_queue_item(kwargs['queue_id'], progress=progress, save_to_file=False)

                    # cancel process if user requested it via queue
                    if self.processing_queue.cancel_if_canceled(queue_id=kwargs.get('queue_id')):
                        return None

        # if there are no segments to classify, return
        if not classified_segments:
            return None

        # if there are any multi-label passes, go through each segment and check if it's in the list of all the labels
        # but make sure that sufficient number of label groups were provided
        # - as a minimum check first, before matching the labels below
        if multi_label_pass and len(labels) >= len(multi_label_pass):

            classified_segments['_multi_label_pass_'] = []

            # take each label from the multi-label pass list and use it to intersect the classified segments
            for label in multi_label_pass:

                # is this label in the classified segments keys?
                if label not in classified_segments:
                    # if not, skip it
                    logger.warn("Label {} doesn't exist in classified segments: {}\n"
                                "Multi-label pass failed for all segments, since none of them have this label."
                                .format(label, classified_segments.keys()))
                    break

                # simply add the first label to the multi-label pass list
                # we're going to intersect the other labels with this one
                if not classified_segments['_multi_label_pass_']:
                    classified_segments['_multi_label_pass_'] = classified_segments[label]
                    continue

                # intersect the classified_segments['_multi_label_pass_'] with the current label
                classified_segments['_multi_label_pass_'] = [item for item in classified_segments['_multi_label_pass_']
                                                             if item in classified_segments[label]]

        logger.debug('Classification complete.')

        return classified_segments

    def group_questions(self, transcription_file_path: str = None, group_name: str = "Questions",
                        **kwargs):
        """
        This uses the classify_segments() method to detect questions and add them to a transcription group
        :param transcription_file_path: the path to the transcription json file
        :param group_name: the name of the group to save the questions in (default: Questions)
        :param kwargs: this is not needed, but makes sure that any additional arguments are ignored
        :return: the questions_group
        """

        # use the transcription class to get the segments
        # this will use an already instantiated transcription object if it exists for this file
        transcription = Transcription(transcription_file_path)

        if not transcription:
            fail_error = 'Unable to group questions - no transcription available: {}.'.format(transcription_file_path)
            logger.error(fail_error)

            if kwargs.get('queue_id'):
                self.processing_queue.update_status(kwargs['queue_id'], status='failed', fail_error=fail_error)

            return None

        # only check non-meta segments
        # the group time intervals may contain the meta segments too depending on their start time,
        # but at least we're not processing them
        segments = TranscriptionUtils.filter_segments(transcription.segments, filter_meta=True)

        # classify the segments as questions or statements
        # but use the existing transcription data if we have it
        classified_question_segments = self.classify_segments(
            segments,
            labels=[
                ['interrogative sentence', 'declarative sentence'],
                ['question', 'statement'],
                ['asking', 'telling'],
                ['ask', 'tell'],
            ],
            multi_label_pass=['interrogative sentence', 'question', 'asking', 'ask'],
            min_confidence=[0.5, 0.5, 0.7, 0.7],
            **kwargs
        )

        # cancel if the process was canceled
        if kwargs.get('queue_id'):
            if self.processing_queue.cancel_if_canceled(queue_id=kwargs.get('queue_id')):
                return None

        # initialize the questions_group
        questions_group = None

        # if we have question segments, create a group with them
        # but save it later, after the transcription is saved
        # since this is a multi_label_pass classification, we're going to use the '_multi_label_pass_' key
        if isinstance(classified_question_segments, dict) \
                and '_multi_label_pass_' in classified_question_segments \
                and len(classified_question_segments['_multi_label_pass_']) > 0:
            # get the time intervals of the question segments
            group_time_intervals = \
                transcription.transcript_segments_to_time_intervals(
                    segments=classified_question_segments['_multi_label_pass_'])

            # prepare the new dict of the new group
            # (this will return a dict looking like this {group_id: group_data})
            questions_group = transcription.prepare_transcript_group(
                group_name=group_name,
                time_intervals=group_time_intervals
            )

        # if this was successful, save the questions group to the transcription json file
        if questions_group is not None and isinstance(questions_group, dict) and transcription is not None:
            # get the id of the questions group
            questions_group_id = list(questions_group.keys())[0]

            # push this change to the toolkit_ops_obj
            transcription.set_transcript_groups(group_id=questions_group_id, transcript_groups=questions_group)

            # save the transcription now, not soon
            # - this ensures that the transcription is saved before notifying observers
            transcription.save_soon(sec=0)

        # if we have a queue_id, update the status to done
        if kwargs.get('queue_id', None):
            self.processing_queue.update_status(queue_id=kwargs.get('queue_id', None), status='done')

        # update all the observers that are listening for this transcription
        self.notify_observers('update_transcription_{}'.format(transcription.transcription_path_id))

        # update all the observers that are listening for this transcription's groups
        self.notify_observers('update_transcription_groups_{}'
                              .format(transcription.transcription_path_id))

        return questions_group

    def add_group_questions_to_queue(self, queue_item_name, transcription_file_path, group_name):

        # prepare the options for the processing queue
        queue_item = dict()
        queue_item['name'] = queue_item_name
        queue_item['source_file_path'] = queue_item['transcription_file_path'] = transcription_file_path
        queue_item['tasks'] = ['group_questions']
        queue_item['device'] = self.torch_device_type_select()
        queue_item['group_name'] = group_name
        queue_item['item_type'] = 'transcription'

        logger.debug('Adding group questions to queue: {}'.format(queue_item_name))

        return self.processing_queue.add_to_queue(**queue_item)

    def add_speaker_detection_to_queue(self, queue_item_name, transcription_file_path, time_intervals, device_name):

        # prepare the options for the processing queue
        queue_item = dict()
        queue_item['name'] = queue_item_name
        queue_item['source_file_path'] = queue_item['transcription_file_path'] = transcription_file_path
        queue_item['tasks'] = ['speaker_detection']
        queue_item['device'] = self.torch_device_type_select(device_name)
        queue_item['time_intervals'] = time_intervals
        queue_item['item_type'] = 'transcription'

        logger.debug('Adding speaker detection to queue: {} ({})'.format(queue_item_name, time_intervals))

        return self.processing_queue.add_to_queue(**queue_item)

    def index_text(self, search_file_paths: list = None, **kwargs):
        """
        This takes the search_file_paths through the TextSearch embedder and saves their cached embeddings to disk
        """

        if kwargs.get('queue_id', None):
            self.processing_queue.update_status(queue_id=kwargs.get('queue_id', None), status='reading files')

        search_item = TextSearch(
            toolkit_ops_obj=self, search_file_paths=search_file_paths, search_type='semantic',
            use_analyzer=kwargs.get('use_analyzer', False)
        )

        # prepare the search corpus
        search_item.prepare_search_corpus()

        # cancel indexing if user requested it
        if self.processing_queue.cancel_if_canceled(queue_id=kwargs.get('queue_id', None)):
            return None

        def batch_progress_callback(current_index, total_indexes):
            """
            This is sent to the encoder to be called after each batch is processed
            and track the progress so we can update the queue item and cancel mid-encoding if requested
            """

            if kwargs.get('queue_id', None) is not None:

                # cancel indexing if user requested it
                if self.processing_queue.cancel_if_canceled(queue_id=kwargs.get('queue_id', None)):
                    return None

                # calculate the current progress in percent
                current_progress = round((current_index / total_indexes) * 100)

                # update the queue item progress
                self.processing_queue.update_queue_item(
                    queue_id=kwargs.get('queue_id', None),
                    status='indexing',
                    progress=current_progress,
                )

            # keep going
            return True

        # embed the search corpus
        if not search_item.embed_corpus(batch_process_callback=batch_progress_callback):
            return None

        if kwargs.get('queue_id', None):
            self.processing_queue.update_status(queue_id=kwargs.get('queue_id', None), status='done')

        # search_item.search_file_path_id
        # notify all observers that are listening for this search_file_path_id
        self.notify_observers('update_done_indexing_search_file_path_{}'.format(search_item.search_file_path_id))

        return True

    def add_index_text_to_queue(self, queue_item_name, search_file_paths):

        # prepare the options for the processing queue
        queue_item = dict()
        queue_item['name'] = queue_item_name
        queue_item['task_data'] = True
        queue_item['tasks'] = ['index_text']
        queue_item['device'] = self.torch_device_type_select()
        queue_item['item_type'] = 'search'
        queue_item['search_file_paths'] = search_file_paths

        # get the search_file_path_id from the TextSearch object
        search_item = TextSearch(toolkit_ops_obj=self, search_file_paths=search_file_paths, search_type='semantic')

        queue_item['use_analyzer'] = search_item.use_analyzer

        # this will be used to notify observers when the indexing has been stopped for any reason
        queue_item['on_stop_action_name'] \
            = 'update_fail_indexing_search_file_path_{}'.format(search_item.search_file_path_id)

        return self.processing_queue.add_to_queue(**queue_item)

    def index_video(self, video_file_path, **kwargs):

        indexing_options = kwargs.get('indexing_options', dict())
        detection_options = kwargs.get('detection_options', dict())

        # the target dir is either something that was sent via other_options
        # or it's the directory of the video_file_path
        target_dir = kwargs.get('target_dir', None) or os.path.dirname(video_file_path)

        # the transcription_file_path is either something that was sent via other_options
        # or it's the video_file_path, but with the extension changed to .transcription.json
        transcription_file_path = kwargs.get('transcription_file_path', None) \
            or os.path.join(target_dir, '{}.transcription.json'.format(os.path.basename(video_file_path)))

        # if a transcription_file_path was passed use it,
        # but only if we don't have other transcription file paths
        if kwargs.get('transcription_file_paths', None) is None:
            transcription = Transcription(transcription_file_path)

        # if we have more transcription_file_paths in kwargs, we'll deal with them later
        else:
            transcription = None

        def frame_progress_callback(current_frame, total_frames):
            """
            This is sent to the encoder to be called after each frame was processed
            and track the progress so we can update the queue item and cancel mid-encoding if requested
            """

            if kwargs.get('queue_id', None) is not None:

                # cancel indexing if user requested it
                if self.processing_queue.cancel_if_canceled(queue_id=kwargs.get('queue_id', None)):
                    return None

                # calculate the current progress in percent
                current_progress = round((current_frame / total_frames) * 100)

                # update the queue item progress
                self.processing_queue.update_queue_item(
                    queue_id=kwargs.get('queue_id', None),
                    status='indexing',
                    progress=current_progress,
                )

            # keep going
            return True

        self.processing_queue.update_status(queue_id=kwargs.get('queue_id', None), status='loading')

        # initialize the encoder
        index = ClipIndex(
            path=video_file_path, device=self.torch_device, patch_divider=indexing_options.get('patch_divider', 1.9)
        )

        queue_id = kwargs.get('queue_id', None)

        def detect_progress(**progress_kwargs):

            # calculate the current progress in percent based on where we are in the total number of frames
            # but also divide by 3 because even if STAGE 1 is fully done, we're only 33% done
            progress = int(
                int(progress_kwargs.get('current_frame_index', 1)) / int(progress_kwargs.get('total_frames', 1))
                * 100 // 3)

            # cancel indexing if user requested it
            if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
                return None

            # update status+progress
            self.processing_queue.update_queue_item(
                queue_id=queue_id, status='detecting scenes', progress=progress)

            return True

        # STAGE 1 - detect scenes
        shot_indexes = \
            index.get_scene_changes(frame_progress_callback=detect_progress, **detection_options)

        def analyze_progress(**progress_kwargs):

            # calculate the current progress in percent based on where we are in the total number of frames
            # but also divide by 3 and add 33% because even if STAGE 2 is fully done, we're only 66% done
            progress = int(
                int(progress_kwargs.get('current_frame_index', 1)) / int(progress_kwargs.get('total_frames', 1))
                * 100 // 3) + 33

            # cancel indexing if user requested it
            if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
                return None

            # update status+progress
            self.processing_queue.update_queue_item(
                queue_id=queue_id, status='analyzing scenes', progress=progress)

            return True

        # STAGE 2 - analyze and filter scenes
        detected_shots = \
            index.analyze_neighbor_shots(shot_indexes, path=video_file_path, frame_progress_callback=analyze_progress)

        # unpack the detected_shots if it's a tuple
        if isinstance(detected_shots, tuple) and len(detected_shots) == 2:
            detected_shots, _ = detected_shots

        # otherwise, just stop the process
        else:
            self.processing_queue.cancel_item(queue_id=queue_id)
            return None

        def index_progress(**progress_kwargs):

            # calculate the current progress in percent based on where we are in the total number of frames
            # but also divide by 3 and add 33% because even if STAGE 2 is fully done, we're only 66% done
            progress = int(
                int(progress_kwargs.get('current_frame_index', 1)) / int(progress_kwargs.get('total_frames', 1))
                * 100 // 3) + 66

            # cancel indexing if user requested it
            if self.processing_queue.cancel_if_canceled(queue_id=queue_id):
                return None

            # update status+progress
            self.processing_queue.update_queue_item(
                queue_id=queue_id, status='indexing', progress=progress)

            return True

        # STAGE 3 - index the video
        # index the video and return None if something went wrong
        # if not index.index_video(path=video_file_path, frame_progress_callback=frame_progress_callback,  **kwargs):
        if not index.index_video(
                path=video_file_path, detected_shots=detected_shots,
                skip_empty=2, frame_progress_callback=index_progress, **indexing_options):
            return None

        # cancel indexing if user requested it
        if self.processing_queue.cancel_if_canceled(queue_id=kwargs.get('queue_id', None)):
            return None

        # update status
        self.processing_queue.update_status(queue_id=kwargs.get('queue_id', None), status='saving index')

        # save the embeddings to disk
        embedding_paths = index.save_embeddings()

        # if we didn't get the embedding paths, something went wrong
        if not embedding_paths or not isinstance(embedding_paths, tuple) or len(embedding_paths) != 2:
            logger.error('Indexing failed - embedding paths not received.')
            self.processing_queue.update_status(
                queue_id=kwargs.get('queue_id', None),
                status='failed',
                fail_error='Embedding paths not received. See app.log for possible details.'
            )
            return None

        # this is the path to the numpy file which contains the embeddings
        numpy_file_path = embedding_paths[0]

        # if 'transcription_file_paths' exists in kwargs, add the video index paths to all the transcriptions
        # this should be the case if the user transcribed and translated the video before indexing it
        # therefore creating more than one transcription file
        if kwargs.get('transcription_file_paths', None):
            for transcription_file_path in kwargs.get('transcription_file_paths', []):

                # add the video index paths to the transcription and get the transcription object
                self.add_video_index_paths_to_transcription(
                    transcription_file_path=transcription_file_path, video_index_path=numpy_file_path
                )

                # add metadata, but use the path instead of the transcription object
                self.process_transcription_metadata(other_options=kwargs, transcription=transcription_file_path)

        # otherwise, if 'transcription_file_path' exists in the transcription object,
        # add the metadata path to that transcription
        elif transcription is not None and transcription.transcription_file_path is not None:
            transcription = self.add_video_index_paths_to_transcription(
                transcription_file_path=transcription_file_path, video_index_path=numpy_file_path
            )

            # add metadata
            self.process_transcription_metadata(other_options=kwargs, transcription=transcription)

        # delete the render.json file if it exists
        if kwargs.get('source_file_path', None) and kwargs.get('ingest_delete_render_info_file', False):

            source_file_path = kwargs.get('source_file_path', None)
            render_json_file_path = "{}.json".format(source_file_path)

            # delete the render.json file if it exists
            TranscriptionUtils.delete_render_json(render_json_file_path=render_json_file_path)

        # update status
        self.processing_queue.update_status(queue_id=kwargs.get('queue_id', None), status='done')

        return True

    @staticmethod
    def add_video_index_paths_to_transcription(transcription_file_path, video_index_path):
        """
        This adds the video index path (numpy_file_path) to said transcription file
        :param transcription_file_path: the path to the transcription file
        :param video_index_path: the path to the video index file
        :return: the transcription object
        """

        transcription = Transcription(transcription_file_path=transcription_file_path)

        # add the video index paths to the transcription
        transcription.set('video_index_path', os.path.basename(video_index_path))

        # save the transcription
        transcription.save_soon(sec=0)

        return transcription

    def add_index_video_to_queue(self, video_file_path, **other_options):
        """
        This adds the clip indexing task to the processing queue
        """

        # prepare the queue item dict
        queue_item = dict()

        # the queue item name
        queue_item_name = os.path.basename(video_file_path)

        # let's define the source file path as the video file path for now
        queue_item['source_file_path'] = video_file_path

        # the transcription file path
        queue_item['transcription_file_path'] = other_options.get('transcription_file_path', None)

        # the queue id if any
        queue_item['queue_id'] = other_options.get('queue_id', None)

        # other options
        queue_item['name'] = 'Video index of {}'.format(queue_item_name)
        queue_item['task_data'] = True
        queue_item['tasks'] = ['index_video']
        queue_item['device'] = self.torch_device_type_select()
        queue_item['item_type'] = 'index_video'
        queue_item['video_file_path'] = queue_item['source_file_path']

        # metadata stuff
        queue_item['timeline_name'] = other_options.get('timeline_name', None)
        queue_item['project_name'] = other_options.get('project_name', None)
        queue_item['timeline_fps'] = other_options.get('timeline_fps', None)
        queue_item['timeline_start_tc'] = other_options.get('timeline_start_tc', None)
        queue_item['ingest_delete_render_info_file'] = other_options.get('ingest_delete_render_info_file', False)

        queue_item['indexing_options'] = other_options.get('indexing_options', dict())
        queue_item['detection_options'] = other_options.get('detection_options', dict())

        return self.processing_queue.add_to_queue(**queue_item)

    # RESOLVE SPECIFIC METHODS

    def resolve_disable(self):
        """
        This function is used to disable the resolve API
        The polling thread will continue to run, but will not poll for data until the resolve API is re-enabled
        """

        # set the resolve object to None
        self.resolve_api = None

        # set the disable resolve API flag to True
        self.disable_resolve_api = True

        # force the NLE object to None
        NLE.resolve = None
        NLE.reset_all()

        # notify observers that the NLE has been reset
        self.notify_observers('update_NLE_status')

    def resolve_enable(self):
        """
        This function is used to enable the resolve API
        """

        # initialize a resolve object
        if not self.resolve_api or self.resolve_api is None:
            self.resolve_api = MotsResolve(logger=logger)

        self.disable_resolve_api = False

        if not self.polling_resolve:
            logger.debug('Resolve polling thread not detected, starting one now')

            # start the resolve thread
            # with this, resolve should be constantly polled for data
            self.poll_resolve_thread()

    def calculate_sec_to_resolve_timecode(self, seconds=0):

        if NLE.resolve:

            # poll resolve for some info
            # @todo avoid polling resolve for this info and use the existing current_timeline_fps
            #   and current_timeline_startTC
            resolve_data = self.resolve_api.get_resolve_data()

            # get the framerate of the current timeline
            timeline_fps = resolve_data['currentTimelineFPS']

            if timeline_fps is None:
                return False

            # get the start timecode of the current timeline
            timeline_start_tc = resolve_data['currentTimeline']['startTC']

            # initialize the timecode object for the start tc
            timeline_start_tc = Timecode(timeline_fps, start_timecode=timeline_start_tc)

            # only do timecode math if seconds > 0
            if seconds > 0:

                # initialize the timecode object via seconds to frames to tc
                tc_from_seconds = sec_to_tc(seconds=seconds, fps=timeline_fps)

                # calculate the new timecode
                new_timeline_tc = timeline_start_tc + tc_from_seconds

            # otherwise use the timeline start tc
            else:
                new_timeline_tc = timeline_start_tc

            return new_timeline_tc

        else:
            return False

    def calculate_resolve_timecode_to_sec(self, timecode=None, frames=None, framerate=None, start_tc=None):
        '''
        Calculates the seconds from a timecode or frames based on the current timeline's framerate

        :param timecode: The timecode to calculate the seconds from
        :param frames: The number of frames to calculate the seconds from
        :param frame rate: The framerate of the timeline in FPS
        :param start_tc: The start timecode of the timeline
        :return:
        '''

        if NLE.resolve:

            # poll resolve for some info
            # @todo avoid polling resolve for this info and use the existing current_timeline_fps
            #   and current_timeline_startTC
            resolve_data = self.resolve_api.get_resolve_data()

            # get the framerate of the current timeline
            # either from Resolve...
            if 'currentTimelineFPS' in resolve_data:
                timeline_fps = resolve_data['currentTimelineFPS']
            # ...from the passed framerate
            elif framerate is not None:
                timeline_fps = framerate
            # ...or abort
            else:
                logger.debug('No timeline framerate found. Aborting.')
                return None

            # get the start timecode of the current timeline
            if 'currentTimeline' in resolve_data and 'startTC' in resolve_data['currentTimeline']:
                timeline_start_tc = resolve_data['currentTimeline']['startTC']
            elif start_tc is not None:
                timeline_start_tc = start_tc
            else:
                timeline_start_tc = '00:00:00:00'

            # initialize the timecode object for the start tc
            timeline_start_tc = Timecode(timeline_fps, timeline_start_tc)

            # if no timecode was passed, try to get it from the NLE object
            if timecode is None and frames is None:
                timecode = NLE.current_tc

            # calculate the timecode from the passed frames
            if frames is not None:
                timecode = Timecode(framerate=timeline_fps, frames=frames)

            # if we still don't have a timecode, abort and return None
            if timecode is None:
                logger.debug('No timecode was passed. Aborting.')
                return None

            # initialize the timecode object for the passed timecode
            tc = Timecode(timeline_fps, timecode)

            # calculate the difference between the start tc and the passed tc
            tc_diff = tc - timeline_start_tc

            # calculate the seconds from the timecode frames
            tc_diff_seconds = tc_diff.frames / timeline_fps

            # return the seconds which is the previous calculated difference
            return tc_diff_seconds

        else:
            return None

    def go_to_time(self, seconds=0):

        if NLE.resolve:
            new_timeline_tc = self.calculate_sec_to_resolve_timecode(seconds)

            # move playhead in resolve
            self.resolve_api.set_resolve_tc(str(new_timeline_tc))

    def poll_resolve_thread(self):
        '''
        This keeps resolve polling in a separate thread
        '''

        # don't start this if another thread is already running
        if self.polling_resolve:
            logger.debug('Resolve polling thread already running')
            return

        # wrap poll_resolve_data into a thread
        poll_resolve_thread = Thread(target=self.poll_resolve_data)

        # stop the thread when the main thread stops
        poll_resolve_thread.daemon = True

        # start the thread
        poll_resolve_thread.start()

    def poll_resolve_data(self):
        '''
        Polls resolve and returns either the data passed from resolve, or False if any exceptions occurred
        :return:
        '''

        # don't start this if another thread is already running
        if self.polling_resolve:
            logger.debug('Resolve polling thread already running')
            return

        # do this continuously
        while True:

            # keep updating the resolve_poll_num
            NLE.resolve_poll_num += 1

            polling_start_time = time.time()

            # make sure everyone knows that this loop is still running
            # (and implicitly the thread too)
            self.polling_resolve = True

            # if polling is not suspended temporarily
            if not NLE.suspend_polling:

                # try to poll resolve
                try:

                    # check if the resolve api is not disabled
                    if self.disable_resolve_api:
                        logger.debug('Resolve API disabled. Aborting Resolve API polling until re-enabled.')
                        self.polling_resolve = False

                        # execute this one more time to make sure all variables are set correctly
                        self.resolve_disable()

                        return None

                    # also, check if the API module is available on this machine
                    if not self.resolve_api.api_module_available:
                        logger.debug('Resolve API module not available on this machine. '
                                     'Aborting Resolve API polling until StoryToolkitAI restart.')
                        self.polling_resolve = False
                        return None

                    # actual polling happens here
                    resolve_data = self.resolve_api.get_resolve_data(silent=True)

                    # for all the NLE variables related with resolve data,
                    #  check if the data has changed and if so, update the NLE variable
                    #  but if the polled data does not contain the key, also set the NLE variable to None
                    #  also, if the global variable is not None and the polled data doesn't contain the key,
                    #  set the global variable to None
                    # also, make sure you notify the relevant observers that the data has changed

                    # RESOLVE OBJECT CHANGE
                    # if the resolve object has changed (for eg. from None to an object)
                    try:
                        if type(NLE.resolve) != type(resolve_data['resolve']):

                            logger.debug('Resolve object changed from {} to {}.'
                                         .format(type(NLE.resolve), type(resolve_data['resolve'])))

                            # set the resolve object to whatever it is now
                            NLE.resolve = resolve_data['resolve']

                            # notify the observers that the resolve object has changed
                            self.notify_observers('update_NLE_status')
                            self.notify_observers('update_all_transcriptions')

                            # if the resolve object is now None,
                            # reset all and skip the rest of the polling
                            if NLE.resolve is None:
                                self.notify_observers('NLE_project_changed')
                                self.notify_observers('NLE_timeline_changed')

                                NLE.reset_all()
                    except:
                        import traceback
                        logger.debug('Fail detected in resolve object change check.')
                        logger.debug(traceback.format_exc())
                        continue

                    # RESOLVE TIMELINE NAME CHANGE
                    # if the current project was already set and now it's no longer set in the polled data
                    # or if the current project has changed
                    try:
                        if (NLE.current_project is not None and 'currentProject' not in resolve_data) \
                                or NLE.current_project != resolve_data['currentProject']:
                            # logger.debug('Current project changed from {} to {}.'
                            #             .format(NLE.current_project, resolve_data['currentProject']))

                            # set the current project to whatever it is now
                            # but if the polled data doesn't contain the currentProject key, set it to None
                            NLE.current_project = resolve_data[
                                'currentProject'] if 'currentProject' in resolve_data else None

                            # notify the observers that the project has changed
                            self.notify_observers('NLE_project_changed')
                            self.notify_observers('update_all_transcriptions')

                    except Exception as e:
                        logger.debug(e)
                        logger.debug('Fail detected in resolve project change check.', exc_info=True)
                        continue

                    # if the current timeline was already set and now it's no longer set in the polled data
                    # or if the current timeline has changed
                    try:
                        if (NLE.current_timeline is not None and 'currentTimeline' not in resolve_data) \
                                or NLE.current_timeline != resolve_data['currentTimeline']:

                            # logger.debug('Current timeline changed from {} to {}.'
                            #             .format(NLE.current_timeline, resolve_data['currentTimeline']))

                            # because we only want to trigger the timeline_changed event
                            # if the name of the timeline has changed
                            # but resolve_data['currentTimeline'] contains the entire timeline object,
                            # including markers and other data that may have changed,
                            # we need to focus on the name of the timeline

                            # but first, if the polled data doesn't contain the currentTimeline key
                            # yet the NLE.current_timeline was set before
                            if NLE.current_timeline is not None and 'currentTimeline' not in resolve_data:

                                # logger.debug('Current timeline changed from {} to None.'.format(NLE.current_timeline))

                                # set the current timeline to None
                                NLE.current_timeline = None

                                self.notify_observers('NLE_timeline_changed')

                            # if the polled data contains the currentTimeline key
                            elif 'currentTimeline' in resolve_data \
                                    and (type(NLE.current_timeline) != type(resolve_data['currentTimeline']) \
                                         or ('name' in NLE.current_timeline and not 'name' in resolve_data[
                                        'currentTimeline']) \
                                         or ('name' in resolve_data[
                                        'currentTimeline'] and not 'name' in NLE.current_timeline) \
                                         or (NLE.current_timeline['name'] != resolve_data['currentTimeline']['name'])):

                                # logger.debug('Current timeline changed from {} to {}.'
                                #             .format(NLE.current_timeline, resolve_data['currentTimeline']))

                                # set the current timeline to whatever it is now
                                NLE.current_timeline = resolve_data['currentTimeline'] \
                                    if 'currentTimeline' in resolve_data else None

                                # and notify the observers that the timeline has changed
                                self.notify_observers('NLE_timeline_changed')
                                self.notify_observers('NLE_timecode_data_changed')

                            # if the polled data contains the currentTimeline key,
                            # but the name of the timeline hasn't changed
                            else:
                                # set the current timeline to whatever it is now
                                # but don't trigger the timeline_changed event
                                NLE.current_timeline = resolve_data['currentTimeline'] \
                                    if 'currentTimeline' in resolve_data else None

                    except Exception as e:
                        logger.debug(e)
                        logger.debug('Fail detected in resolve timeline change check.', exc_info=True)
                        continue

                    # did the markers change?
                    # (this only matters if the current timeline is not None
                    # and if the current timeline has markers)
                    if resolve_data is not None and type(resolve_data) is dict \
                            and 'currentTimeline' in resolve_data \
                            and type(resolve_data['currentTimeline']) is dict \
                            and 'markers' in resolve_data['currentTimeline']:

                        # first compare the types
                        if type(NLE.current_timeline_markers) != type(resolve_data['currentTimeline']['markers']):
                            # if the types are different, then the markers have changed
                            self.notify_observers('NLE_markers_changed')

                            NLE.current_timeline_markers = resolve_data['currentTimeline']['markers']

                        # also do a key compare only for speed
                        elif set(NLE.current_timeline_markers.keys()) != set(
                                resolve_data['currentTimeline']['markers'].keys()):
                            # if the keys are different, then the markers have changed
                            self.notify_observers('NLE_markers_changed')

                            NLE.current_timeline_markers = resolve_data['currentTimeline']['markers']

                        # but if the marker keys are the same do a deeper compare
                        elif NLE.current_timeline_markers != resolve_data['currentTimeline']['markers']:
                            # if the keys are the same, but the values are different, then the markers have changed
                            self.notify_observers('NLE_markers_changed')

                            NLE.current_timeline_markers = resolve_data['currentTimeline']['markers']

                    else:
                        NLE.current_timeline_markers = None

                    #  updates the currentBin
                    if (NLE.current_bin is not None and NLE.current_bin != '' and 'currentBin' not in resolve_data) \
                            or NLE.current_bin != resolve_data['currentBin']:
                        NLE.current_bin = resolve_data['currentBin'] if 'currentBin' in resolve_data else ''
                        self.notify_observers('NLE_bin_changed')

                    # update current playhead timecode
                    if (NLE.current_tc is not None and 'currentTC' not in resolve_data) \
                            or NLE.current_tc != resolve_data['currentTC']:
                        NLE.current_tc = resolve_data['currentTC']
                        self.notify_observers('NLE_tc_changed')

                    # update current playhead timecode
                    if (NLE.current_timeline_fps is not None and 'currentTimelineFPS' not in resolve_data) \
                            or NLE.current_timeline_fps != resolve_data['currentTimelineFPS']:
                        NLE.current_timeline_fps = resolve_data['currentTimelineFPS']
                        self.notify_observers('NLE_timecode_data_changed')

                    # update start_tc timecode
                    if (NLE.current_start_tc is not None
                        and 'currentTimeline' not in resolve_data
                        and 'startTC' not in resolve_data['currentTimeline']) \
                            or (resolve_data['currentTimeline'] is not None \
                            and NLE.current_start_tc != resolve_data['currentTimeline']['startTC']):
                        NLE.current_start_tc = \
                            resolve_data['currentTimeline']['startTC'] if isinstance(resolve_data, dict) else None
                        self.notify_observers('NLE_timecode_data_changed')

                    # was there a previous error?
                    if NLE.resolve is not None and NLE.resolve_error > 0:
                        # first let the user know that the connection is back on
                        logger.warning("Resolve connection re-established.")

                        # reset the error counter
                        NLE.resolve_error = 0

                    elif NLE.resolve is None:
                        NLE.resolve_error += 1

                # if an exception is thrown while trying to work with Resolve, don't crash, but continue to try to poll
                except Exception as e:

                    logger.debug(e)
                    logger.debug('Fail detected in resolve polling loop.', exc_info=True)

                    # count the number of errors
                    NLE.resolve_error += 1

                # how often do we poll resolve?
                polling_interval = 500

                # if any errors occurred
                if NLE.resolve_error:

                    # let the user know that there's an error, and throttle the polling_interval

                    # after 15+ errors, deduce that Resolve will not be started this session, so stop polling
                    if NLE.resolve_error > 15:
                        logger.warning("Resolve not reachable after 15 tries. "
                                       "Disabling Resolve API polling until tool restart.")

                        polling_interval = None

                    # after 10+ tries, assume the user is no longer paying attention and reduce the frequency of tries
                    elif NLE.resolve_error > 10:

                        # only show this error one more time
                        if NLE.resolve_error == 11:
                            logger.debug('Resolve is still not reachable. Throttling polling interval to 15 seconds.')

                        # and increase the polling interval to 15 seconds
                        polling_interval = 15000

                    # if the error has been triggered more than 5 times, do this
                    elif NLE.resolve_error > 5:

                        if NLE.resolve_error == 11:
                            logger.warning('Resolve is still not reachable. Now retrying every 5 seconds or so.')

                        # increase the polling interval to 5 seconds
                        polling_interval = 5000

                    else:
                        if NLE.resolve_error == 1:
                            logger.warning('Resolve is not open or reachable. Retrying every few seconds.')

                        # increase the polling interval to 1 second
                        polling_interval = 1000

                    # calculate the time that has passed since the polling started
                    polling_time = time.time() - polling_start_time

                    # if the polling time takes longer than 1 second, throttle the polling interval
                    if polling_interval is not None and polling_time > 1:
                        polling_interval = polling_interval + polling_time

                    # logger.debug('Polling time: {} seconds'.format(round(time.time() - polling_start_time), 2))

                if polling_interval is None:
                    self.polling_resolve = False
                    return False

                # take a short break before continuing the loop
                time.sleep(polling_interval / 1000)

            else:
                # take a 0.5-second break before trying this again
                time.sleep(0.5)

    def resolve_check_timeline(self, resolve_data, toolkit_UI_obj):
        '''
        This checks if a timeline is available
        :param resolve:
        :return: bool
        '''

        # trigger warning if there is no current timeline
        if resolve_data['currentTimeline'] is None:
            toolkit_UI_obj.notify_via_messagebox(
                message='Timeline not available. Make sure that you\'ve opened a Timeline in Resolve.',
                level='warning')
            return False

        else:
            return True

    def are_files_in_dir(self, dir, files_present):
        """
        This looks for a list of files in a directory and returns True if they're all present
        """
        return all(os.path.exists(os.path.join(dir, file)) for file in files_present)

    def start_resolve_render_and_monitor(self, monitor_callback: callable = None, **kwargs):
        """
        This starts a render in Resolve and then uses a monitor to check if it's done
        :param monitor_callback: a callable that will be called when the render is done
        """

        if not kwargs.get('target_dir', None) or not kwargs.get('file_name', None):
            logger.error('Cannot start render. Missing target_dir or file_name.')
            return False

        # start the timeline Render in Resolve via CLI and pass all the kwargs
        # get the render info so we can pass it to the Monitor object
        render_info = self.render_timeline_via_cli(**kwargs)

        if not render_info or not isinstance(render_info, list):
            logger.error('Cannot monitor render - Resolve returned an unexpected value instead of the render info.')
            return False

        # if the render info is a list, we have multiple renders, so we need to monitor all the files
        monitor_file_paths = []
        render_file_paths = []

        for render_job in render_info:

            if 'OutputFilename' in render_job:

                # we need the file path for the return value
                render_file_paths.append(os.path.join(render_job['TargetDir'], render_job['OutputFilename']))

                # when mots_resolve finishes the render job,
                # it should also add a .json file with the same name,
                # so we'll just monitor to see if the .json file is there
                # monitoring the rendered file itself not might be a good idea
                # since it might already exist before the render is done
                # we will call this file the render.json file
                monitor_file_path = str(render_job['OutputFilename']) + '.json'

                # delete the file we're monitoring if it's already there
                if os.path.exists(os.path.join(kwargs['target_dir'], monitor_file_path)):
                    try:
                        os.remove(os.path.join(kwargs['target_dir'], monitor_file_path))
                    except:
                        logger.error('Cannot monitor render for {} '
                                     '- the file {} already exists and cannot be deleted.'
                                     .format(render_job['OutputFilename'],
                                             os.path.join(kwargs['target_dir'], monitor_file_path)))
                        continue

                monitor_file_paths.append(monitor_file_path)

        if not monitor_file_paths or len(monitor_file_paths) == 0:
            logger.error('Cannot monitor render - no files to monitor.')
            return False

        # add a monitor to check if the render is done
        # don't forget to add the "done" callback outside this function
        # if we haven't added it via monitor_callback already!!
        monitor = Monitor(
            done=monitor_callback,
            condition=lambda: self.are_files_in_dir(kwargs['target_dir'], monitor_file_paths)
        )

        # return the files list so we can use them further down the line
        return monitor, render_file_paths

    def render_timeline_via_cli(self, target_dir, render_preset, file_name, **kwargs):
        """
        This renders the current timeline via the CLI to avoid blocking the UI.
        """
        # first, add the render job to the render queue
        # we do it directly, not through CLI because we need to get the render job ID

        # start_render must be False
        kwargs['start_render'] = False
        kwargs['return_render_job'] = True

        # add the render job to the Resolve render queue
        render_job_info = self.resolve_api.render_timeline(
            target_dir=target_dir, render_preset=render_preset, file_name=file_name, **kwargs)

        # the render job id should be the first JobId of the first dict of the list
        try:
            render_job_id = render_job_info[0]['JobId']
            render_job_render_data = render_job_info[0]['render_data']

            # if the render job ID is not available, abort
            if not render_job_info:
                logger.error("Cannot render timeline via CLI - render job ID {} is not available."
                             .format(render_job_id))
                return False

            logger.debug("Sending resolve_render command via CLI")

            # instead of py, use the python executable that is running this code to make sure we're in the same env
            python_executable = sys.executable

            # get the path to the script that is running this code
            main_script_path = os.path.realpath(sys.argv[0])

            command = [python_executable, main_script_path, '--mode', 'cli',
                      '--output-dir', '"{}"'.format(target_dir),
                      '--resolve-render-job', render_job_id,
                      '--resolve-render-data', '"{}"'.format(json.dumps(render_job_render_data))]

            # if we're on Windows and this is a standalone build, we don't need the python executable
            if sys.platform == 'win32' and self.stAI.standalone:

                # remove the python executable from the command
                command.pop(0)

            elif sys.platform == 'darwin' and self.stAI.standalone:

                # remove the python executable from the command
                command.pop(0)

                # and replace the main script path with the app bundle path
                # since sys.argv[0] normally points to the script path (for e.g. __main__.py),
                # we need to use the ARGVZERO that was hopefully packed in the environment variables while freezing
                bundle_exec = os.environ.get('ARGVZERO', None)
                if bundle_exec is None:
                    logger.error('Cannot render timeline via CLI - ARGVZERO not available.')
                    return False

                # and then we can get the path to the main script
                command[0] = bundle_exec

            logger.debug('Executing CLI command {}'.format(' '.join(command)))

            # start the render process via CLI
            process = subprocess.Popen(command)

            def check_process(wait_time=2):
                """
                This function checks if the render command was successful
                """
                # wait a bit before checking if the process is still running
                time.sleep(wait_time)

                check_times = 0

                # if the process is still running, check again after a bit
                while process.poll() is None and check_times < 10:
                    time.sleep(wait_time)
                    check_times += 1

                if check_times >= 10:
                    logger.info("CLI subprocess is still running after {} seconds. Assuming render is running. "
                                "Aborting check function to save resources.".format(wait_time * check_times))

                # check one more time if the process is still running
                exit_code = process.poll()
                if exit_code != 0 and exit_code is not None:
                    logger.error(f"CLI subprocess exited with error code {exit_code}")

            # Start the subprocess check in a separate thread
            thread = Thread(target=check_process)
            thread.start()

            # return the render job info
            return render_job_info

        except:
            logger.error("An error occurred while trying to render timeline via CLI.", exc_info=True)
            return False

    def execute_resolve_operation(self, operation, toolkit_UI_obj):
        """
        This executes a given Resolve API operation
        """

        if not operation or operation == '':
            return False

        stAI = self.stAI

        # get info from resolve for later
        resolve_data = self.resolve_api.get_resolve_data()

        # copy markers operation
        if operation == 'copy_markers_timeline_to_clip' or operation == 'copy_markers_clip_to_timeline':

            # set source and destination depending on the operation
            if operation == 'copy_markers_timeline_to_clip':
                source = 'timeline'
                destination = 'clip'

            elif operation == 'copy_markers_clip_to_timeline':
                source = 'clip'
                destination = 'timeline'

            # this else will never be triggered but let's leave it here for safety for now
            else:
                return False

            # trigger warning and stop if there is no current timeline
            if not self.resolve_check_timeline(resolve_data, toolkit_UI_obj):
                return False

            # trigger warning and stop if there are no bin clips
            if resolve_data['binClips'] is None:
                toolkit_UI_obj.notify_via_messagebox(
                    message='Bin clips not available. Make sure that a bin is opened in Resolve.\n\n'
                            'This doesn\'t work if multiple bins or smart bins are selected due to API.',
                    level='warning')
                return False

            # execute operation without asking for any prompts
            # this will delete the existing clip/timeline destination markers,
            # but the user can undo the operation from Resolve
            return self.resolve_api.copy_markers(source, destination,
                                                 resolve_data['currentTimeline']['name'],
                                                 resolve_data['currentTimeline']['name'],
                                                 True)

        # render marker operation
        elif operation == 'render_markers_to_stills' or operation == 'render_markers_to_clips':

            # ask user for marker color
            # or what the marker name starts with

            # but first make a list of all the available marker colors based on the timeline markers
            current_timeline_marker_colors = []
            if self.resolve_check_timeline(resolve_data, toolkit_UI_obj) and \
                    NLE.current_timeline and 'markers' in NLE.current_timeline:
                # take each marker from timeline and get its color
                # but also add a an empty string to the list to allow the user to render all markers
                current_timeline_marker_colors = [' '] + sorted(
                    list(set([NLE.current_timeline['markers'][marker]['color']
                              for marker in NLE.current_timeline['markers']])))

            # if no markers exist, cancel operation and let the user know that there are no markers to render
            marker_color = None
            starts_with = None
            if current_timeline_marker_colors:

                # create a list of widgets for the input dialogue
                input_widgets = [
                    {'name': 'starts_with', 'label': 'Starts With:', 'type': 'entry', 'default_value': ''},
                    {'name': 'color', 'label': 'Color:', 'type': 'option_menu', 'default_value': 'Blue',
                     'options': current_timeline_marker_colors}
                ]

                # then we call the ask_dialogue function
                user_input = self.toolkit_UI_obj.AskDialog(title='Markers to Render',
                                                           input_widgets=input_widgets,
                                                           parent=self.toolkit_UI_obj.root,
                                                           toolkit_UI_obj=self.toolkit_UI_obj,
                                                           ).value()

                # if the user didn't cancel the operation
                if user_input:
                    starts_with = user_input['starts_with'] if user_input['starts_with'] else None
                    marker_color = user_input['color'] if user_input['color'] != ' ' else None

            else:
                no_markers_alert = 'The timeline doesn\'t contain any markers'
                logger.warning(no_markers_alert)
                return False

            if not marker_color and not starts_with:
                logger.debug("User canceled Resolve render operation by mentioning which markers.")
                return False

            if marker_color and marker_color not in current_timeline_marker_colors:
                toolkit_UI_obj.notify_via_messagebox(title='Unavailable marker color',
                                                     message='The marker color you\'ve entered doesn\'t exist on the timeline.',
                                                     message_log="Aborting. User entered a marker color that doesn't exist on the timeline.",
                                                     level='error'
                                                     )

                return False

            render_target_dir = toolkit_UI_obj.ask_for_target_dir()

            if not render_target_dir or render_target_dir == '':
                logger.debug("User canceled Resolve render operation")
                return False

            if operation == 'render_markers_to_stills':
                stills = True
                render = True
                render_preset = "Still_TIFF"
            else:
                stills = False
                render = False
                render_preset = False

            self.resolve_api.render_markers(marker_color, render_target_dir, False, stills, render, render_preset,
                                            starts_with=starts_with)

        return False
