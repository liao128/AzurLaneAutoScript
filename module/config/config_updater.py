import re
from copy import deepcopy

from cached_property import cached_property

from module.base.timer import timer
from module.config.utils import *
from module.logger import logger
from module.redirect_utils.shop_filter import bp_redirect

CONFIG_IMPORT = '''
import datetime

# This file was automatically generated by module/config/config_updater.py.
# Don't modify it manually.


class GeneratedConfig:
    """
    Auto generated configuration
    """
'''.strip().split('\n')
ARCHIVES_PREFIX = {
    'cn': '档案 ',
    'en': 'archives ',
    'jp': '檔案 ',
    'tw': '檔案 '
}


class Event:
    def __init__(self, text):
        self.date, self.directory, self.name, self.cn, self.en, self.jp, self.tw \
            = [x.strip() for x in text.strip('| \n').split('|')]

        self.directory = self.directory.replace(' ', '_')
        self.cn = self.cn.replace('、', '')
        self.en = self.en.replace(',', '').replace('\'', '').replace('\\', '')
        self.jp = self.jp.replace('、', '')
        self.tw = self.tw.replace('、', '')
        self.is_war_archives = self.directory.startswith('war_archives')
        self.is_raid = self.directory.startswith('raid_')
        for server_ in ARCHIVES_PREFIX.keys():
            if self.__getattribute__(server_) == '-':
                self.__setattr__(server_, None)
            else:
                if self.is_war_archives:
                    self.__setattr__(server_, ARCHIVES_PREFIX[server_] + self.__getattribute__(server_))

    def __str__(self):
        return self.directory

    def __eq__(self, other):
        return str(self) == str(other)


class ConfigGenerator:
    @cached_property
    def argument(self):
        """
        Load argument.yaml, and standardise its structure.

        <group>:
            <argument>:
                type: checkbox|select|textarea|input
                value:
                option: Options, if argument has any options.
        """
        data = {}
        raw = read_file(filepath_argument('argument'))
        for path, value in deep_iter(raw, depth=2):
            arg = {
                'type': 'input',
                'value': '',
                # option
            }
            if not isinstance(value, dict):
                value = {'value': value}
            arg['type'] = data_to_type(value, arg=path[1])
            arg.update(value)
            deep_set(data, keys=path, value=arg)

        return data

    @cached_property
    def task(self):
        """
        <task>:
            - <group>
        """
        return read_file(filepath_argument('task'))

    @cached_property
    def override(self):
        """
        <task>:
            <group>:
                <argument>: value
        """
        return read_file(filepath_argument('override'))

    @cached_property
    def gui(self):
        """
        <i18n_group>:
            <i18n_key>: value, value is None
        """
        return read_file(filepath_argument('gui'))

    @cached_property
    @timer
    def args(self):
        """
        Merge definitions into standardised json.

            task.yaml ---+
        argument.yaml ---+-----> args.json
        override.yaml ---+

        """
        # Construct args
        data = {}
        for task, groups in self.task.items():
            for group in groups:
                if group not in self.argument:
                    logger.warning(f'`{task}.{group}` is not related to any argument group')
                    continue
                deep_set(data, keys=[task, group], value=deepcopy(self.argument[group]))

        # Override non-modifiable arguments
        for path, value in deep_iter(self.override, depth=3):
            # Check existence
            old = deep_get(data, keys=path, default=None)
            if old is None:
                logger.warning(f'`{".".join(path)}` is not a existing argument')
                continue
            # Check type
            # But allow `Interval` to be different
            old_value = old.get('value', None) if isinstance(old, dict) else old
            if type(value) != type(old_value) and path[2] not in ['SuccessInterval', 'FailureInterval']:
                logger.warning(
                    f'`{value}` ({type(value)}) and `{".".join(path)}` ({type(old_value)}) are in different types')
                continue
            # Check option
            if isinstance(old, dict) and 'option' in old:
                if value not in old['option']:
                    logger.warning(f'`{value}` is not an option of argument `{".".join(path)}`')
                    continue

            deep_set(data, keys=path + ['value'], value=value)
            deep_set(data, keys=path + ['type'], value='disable')

        # Set command
        for task in self.task.keys():
            if deep_get(data, keys=f'{task}.Scheduler.Command'):
                deep_set(data, keys=f'{task}.Scheduler.Command.value', value=task)
                deep_set(data, keys=f'{task}.Scheduler.Command.type', value='disable')

        return data

    @timer
    def generate_code(self):
        """
        Generate python code.

        args.json ---> config_generated.py

        """
        visited_group = set()
        visited_path = set()
        lines = CONFIG_IMPORT
        for path, data in deep_iter(self.argument, depth=2):
            group, arg = path
            if group not in visited_group:
                lines.append('')
                lines.append(f'    # Group `{group}`')
                visited_group.add(group)

            option = ''
            if 'option' in data and data['option']:
                option = '  # ' + ', '.join([str(opt) for opt in data['option']])
            path = '.'.join(path)
            lines.append(f'    {path_to_arg(path)} = {repr(parse_value(data["value"], data=data))}{option}')
            visited_path.add(path)

        with open(filepath_code(), 'w') as f:
            for text in lines:
                f.write(text + '\n')

    @timer
    def generate_i18n(self, lang):
        """
        Load old translations and generate new translation file.

                     args.json ---+-----> i18n/<lang>.json
        (old) i18n/<lang>.json ---+

        """
        new = {}
        old = read_file(filepath_i18n(lang))

        def deep_load(keys, default=True, words=('name', 'help')):
            for word in words:
                k = keys + [str(word)]
                d = ".".join(k) if default else str(word)
                value = deep_get(old, keys=k, default=d)
                deep_set(new, keys=k, value=value)

        # Menu
        for path, data in deep_iter(self.menu, depth=2):
            func, group = path
            deep_load(['Menu', func])
            deep_load(['Menu', group])
            for task in data:
                deep_load([func, task])
        # Arguments
        visited_group = set()
        for path, data in deep_iter(self.argument, depth=2):
            if path[0] not in visited_group:
                deep_load([path[0], '_info'])
                visited_group.add(path[0])
            deep_load(path)
            if 'option' in data:
                deep_load(path, words=data['option'], default=False)
        # Event names
        # Names come from SameLanguageServer > en > cn > jp > tw
        events = {}
        for event in self.event:
            if lang in LANG_TO_SERVER:
                name = event.__getattribute__(LANG_TO_SERVER[lang])
                if name:
                    deep_default(events, keys=event.directory, value=name)
        for server_ in ['en', 'cn', 'jp', 'tw']:
            for event in self.event:
                name = event.__getattribute__(server_)
                if name:
                    deep_default(events, keys=event.directory, value=name)
        for event in self.event:
            name = events.get(event.directory, event.directory)
            deep_set(new, keys=f'Campaign.Event.{event.directory}', value=name)
        # GUI i18n
        for path, _ in deep_iter(self.gui, depth=2):
            group, key = path
            deep_load(keys=['Gui', group], words=(key,))

        write_file(filepath_i18n(lang), new)

    @cached_property
    def menu(self):
        """
        Generate menu definitions

        task.yaml --> menu.json

        """
        data = {}

        # Task menu
        group = ''
        tasks = []
        with open(filepath_argument('task'), 'r', encoding='utf-8') as f:
            for line in f.readlines():
                line = line.strip('\n')
                if '=====' in line:
                    if tasks:
                        deep_set(data, keys=f'Task.{group}', value=tasks)
                    group = line.strip('#=- ')
                    tasks = []
                if group:
                    if line.endswith(':'):
                        tasks.append(line.strip('\n=-#: '))
        if tasks:
            deep_set(data, keys=f'Task.{group}', value=tasks)

        return data

    @cached_property
    @timer
    def event(self):
        """
        Returns:
            list[Event]: From latest to oldest
        """
        events = []
        with open('./campaign/Readme.md', encoding='utf-8') as f:
            for text in f.readlines():
                if re.search('\d{8}', text):
                    event = Event(text)
                    events.append(event)

        return events[::-1]

    def insert_event(self):
        """
        Insert event information into `self.args`.

        ./campaign/Readme.md -----+
                                  v
                   args.json -----+-----> args.json
        """
        for event in self.event:
            for server_ in ARCHIVES_PREFIX.keys():
                name = event.__getattribute__(server_)

                def insert(key):
                    options = deep_get(self.args, keys=f'{key}.Campaign.Event.option')
                    if event not in options:
                        options.append(event)
                    if name:
                        deep_default(self.args, keys=f'{key}.Campaign.Event.{server_}', value=event)

                if name:
                    if event.is_raid:
                        insert('Raid')
                        insert('RaidDaily')
                    elif event.is_war_archives:
                        insert('WarArchives')
                    else:
                        insert('Event')
                        insert('EventAb')
                        insert('EventCd')
                        insert('EventSp')
                        insert('GemsFarming')

        # Remove campaign_main from event list
        for task in ['Event', 'EventAb', 'EventCd', 'EventSp', 'Raid', 'RaidDaily', 'WarArchives']:
            options = deep_get(self.args, keys=f'{task}.Campaign.Event.option')
            options = [option for option in options if option != 'campaign_main']
            deep_set(self.args, keys=f'{task}.Campaign.Event.option', value=options)

    @timer
    def generate(self):
        _ = self.args
        _ = self.menu
        _ = self.event
        self.insert_event()
        write_file(filepath_args(), self.args)
        write_file(filepath_args('menu'), self.menu)
        self.generate_code()
        for lang in LANGUAGES:
            self.generate_i18n(lang)


class ConfigUpdater:
    # source, target, (optional)convert_func
    redirection = [
        ('OpsiDaily.OpsiDaily.BuySupply', 'OpsiShop.Scheduler.Enable'),
        ('OpsiDaily.Scheduler.Enable', 'OpsiDaily.OpsiDaily.DoMission'),
        ('OpsiShop.Scheduler.Enable', 'OpsiShop.OpsiShop.BuySupply'),
        ('ShopOnce.GuildShop.Filter', 'ShopOnce.GuildShop.Filter', bp_redirect),
        ('ShopOnce.MedalShop.Filter', 'ShopOnce.MedalShop.Filter', bp_redirect),
    ]

    @cached_property
    def args(self):
        return read_file(filepath_args())

    def read_file(self, config_name):
        """
        Read and update user config.

        Args:
            config_name (str):

        Returns:
            dict:
        """
        new = {}
        old = read_file(filepath_config(config_name))
        is_template = config_name == 'template'

        def deep_load(keys):
            data = deep_get(self.args, keys=keys, default={})
            value = deep_get(old, keys=keys, default=data['value'])
            if value is None or value == '' or data['type'] == 'disable' or is_template:
                value = data['value']
            value = parse_value(value, data=data)
            deep_set(new, keys=keys, value=value)

        for path, _ in deep_iter(self.args, depth=3):
            deep_load(path)

        # AzurStatsID
        if is_template:
            deep_set(new, 'Alas.DropRecord.AzurStatsID', None)
        else:
            deep_default(new, 'Alas.DropRecord.AzurStatsID', random_id())
        # Update to latest event
        server_ = deep_get(new, 'Alas.Emulator.Server', 'cn')
        if not is_template:
            for task in ['Event', 'EventAb', 'EventCd', 'EventSp', 'Raid', 'RaidDaily']:
                deep_set(new,
                         keys=f'{task}.Campaign.Event',
                         value=deep_get(self.args, f'{task}.Campaign.Event.{server_}'))
        # War archive does not allow campaign_main
        for task in ['WarArchives']:
            if deep_get(new, keys=f'{task}.Campaign.Event', default='campaign_main') == 'campaign_main':
                deep_set(new,
                         keys=f'{task}.Campaign.Event',
                         value=deep_get(self.args, f'{task}.Campaign.Event.{server_}'))

        if not is_template:
            new = self.config_redirect(old, new)

        return new

    def config_redirect(self, old, new):
        """
        Convert old settings to the new.

        Args:
            old (dict):
            new (dict):

        Returns:
            dict:
        """
        for row in self.redirection:
            if len(row) == 2:
                source, target = row
                update_func = None
            elif len(row) == 3:
                source, target, update_func = row
            else:
                continue

            value = deep_get(old, keys=source, default=None)
            if value is not None:
                if update_func is not None:
                    value = update_func(value)
                deep_set(new, keys=target, value=value)
            else:
                # No such setting
                continue

        return new

    @timer
    def update_config(self, config_name):
        data = self.read_file(config_name)
        write_file(filepath_config(config_name), data)
        return data


if __name__ == '__main__':
    """
    Process the whole config generation.

                 task.yaml -+----------------> menu.json
             argument.yaml -+-> args.json ---> config_generated.py
             override.yaml -+       |
                  gui.yaml --------\|
                                   ||
    (old) i18n/<lang>.json --------\\========> i18n/<lang>.json
    (old)    template.json ---------\========> template.json
    """
    ConfigGenerator().generate()
    ConfigUpdater().update_config('template')
