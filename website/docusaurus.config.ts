import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Orchestrator',
  tagline:
    'Personal-machine AI orchestrator: free big-AI + local Ollama, RAM-aware',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  url: 'https://skgandikota.github.io',
  baseUrl: '/orchestrator/',

  organizationName: 'skgandikota',
  projectName: 'orchestrator',
  trailingSlash: false,

  onBrokenLinks: 'warn',
  onBrokenMarkdownLinks: 'warn',

  markdown: {
    format: 'detect',
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl:
            'https://github.com/skgandikota/orchestrator/tree/main/website/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/docusaurus-social-card.jpg',
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Orchestrator',
      logo: {
        alt: 'Orchestrator Logo',
        src: 'img/logo.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          href: 'https://github.com/skgandikota/orchestrator#readme',
          label: 'README',
          position: 'right',
        },
        {
          href: 'https://github.com/skgandikota/orchestrator',
          label: 'GitHub',
          position: 'right',
        },
        {
          href: 'https://github.com/skgandikota/orchestrator/blob/main/LICENSE',
          label: 'License',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Introduction', to: '/docs/intro'},
            {label: 'Architecture', to: '/docs/architecture'},
            {label: 'Comparison', to: '/docs/comparison'},
            {label: 'Contributing', to: '/docs/contributing'},
          ],
        },
        {
          title: 'Project',
          items: [
            {
              label: 'README',
              href: 'https://github.com/skgandikota/orchestrator#readme',
            },
            {
              label: 'GitHub',
              href: 'https://github.com/skgandikota/orchestrator',
            },
            {
              label: 'License',
              href: 'https://github.com/skgandikota/orchestrator/blob/main/LICENSE',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} skgandikota — Orchestrator. Licensed CC BY-NC-SA 4.0.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['python', 'bash', 'yaml', 'toml', 'json'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
