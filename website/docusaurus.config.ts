import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Coracle',
  tagline:
    'Personal-machine AI coracle: free big-AI + local Ollama, RAM-aware',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  url: 'https://skgandikota.github.io',
  baseUrl: '/coracle/',

  organizationName: 'skgandikota',
  projectName: 'coracle',
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
            'https://github.com/skgandikota/coracle/tree/main/website/',
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
      title: 'Coracle',
      logo: {
        alt: 'Coracle Logo',
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
          href: 'https://github.com/skgandikota/coracle#readme',
          label: 'README',
          position: 'right',
        },
        {
          href: 'https://github.com/skgandikota/coracle',
          label: 'GitHub',
          position: 'right',
        },
        {
          href: 'https://github.com/skgandikota/coracle/blob/main/LICENSE',
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
              href: 'https://github.com/skgandikota/coracle#readme',
            },
            {
              label: 'GitHub',
              href: 'https://github.com/skgandikota/coracle',
            },
            {
              label: 'License',
              href: 'https://github.com/skgandikota/coracle/blob/main/LICENSE',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} skgandikota — Coracle. Licensed CC BY-NC-SA 4.0.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['python', 'bash', 'yaml', 'toml', 'json'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
