// Spola's overlay icons in Dolphin: a KIO overlay icon plugin, the KDE
// counterpart of extensions/spola-files.py. Same rules: getOverlays() is
// called for every visible item on the GUI thread and answers from a cache;
// a miss asks Spola for the whole folder, asynchronously, and the icons are
// pushed with overlaysChanged() when the answer lands. Without Spola on the
// bus nothing is shown and nothing waits.
//
// D-Bus: io.github.daniel_g_carrasco.spola, /io/github/daniel_g_carrasco/spola/Sync,
// interface io.github.daniel_g_carrasco.spola.Sync1 (see src/spola/dbus_service.py).

#include <KOverlayIconPlugin>

#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusMetaType>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#include <QDBusServiceWatcher>
#include <QDir>
#include <QFileInfo>
#include <QHash>
#include <QSet>
#include <QTimer>
#include <QUrl>

using StateMap = QMap<QString, uint>;
Q_DECLARE_METATYPE(StateMap)

static const QString Service = QStringLiteral("io.github.daniel_g_carrasco.spola");
static const QString ObjectPath = QStringLiteral("/io/github/daniel_g_carrasco/spola/Sync");
static const QString Interface = QStringLiteral("io.github.daniel_g_carrasco.spola.Sync1");
static const int MaxFolders = 400;  // folders kept in the cache, oldest dropped first
static const int MaxSeen = 5000;    // items asked about, to ask again when Spola comes back

// states.State in Spola
static QString emblemFor(uint state)
{
    switch (state) {
    case 1: return QStringLiteral("emblem-spola-synced-symbolic");
    case 2: return QStringLiteral("emblem-spola-syncing-symbolic");
    case 3: return QStringLiteral("emblem-spola-pending-symbolic");
    case 4: return QStringLiteral("emblem-spola-error-symbolic");
    case 5: return QStringLiteral("emblem-spola-conflict-symbolic");
    case 6: return QStringLiteral("emblem-spola-excluded-symbolic");
    default: return QString();
    }
}

// Spola names paths as the system does (/var/home/... on Fedora Atomic)
static QString realPath(const QString &path)
{
    static const QString realHome = QFileInfo(QStringLiteral("/home")).canonicalFilePath();
    if (!realHome.isEmpty() && realHome != QLatin1String("/home") && path.startsWith(QLatin1String("/home/"))) {
        return realHome + path.mid(5);
    }
    return path;
}

class SpolaOverlayPlugin : public KOverlayIconPlugin
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID "io.github.daniel_g_carrasco.spola.overlayiconplugin" FILE "spolaoverlayplugin.json")

public:
    explicit SpolaOverlayPlugin(QObject *parent = nullptr)
        : KOverlayIconPlugin(parent)
        , m_bus(QDBusConnection::sessionBus())
    {
        qDBusRegisterMetaType<StateMap>();
        auto *watcher = new QDBusServiceWatcher(Service, m_bus,
                                                QDBusServiceWatcher::WatchForOwnerChange, this);
        connect(watcher, &QDBusServiceWatcher::serviceOwnerChanged, this,
                [this](const QString &, const QString &, const QString &owner) {
                    m_retry = 0;
                    if (owner.isEmpty()) {
                        forgetAll();
                    } else {
                        fetchRoots();
                    }
                });
        m_bus.connect(Service, ObjectPath, Interface, QStringLiteral("StatesChanged"),
                      this, SLOT(onStatesChanged(QDBusMessage)));
        m_bus.connect(Service, ObjectPath, Interface, QStringLiteral("RootChanged"),
                      this, SLOT(onRootChanged(QString)));
        fetchRoots();
    }

    QStringList getOverlays(const QUrl &item) override
    {
        if (!item.isLocalFile()) {
            return {};
        }
        const QString path = realPath(QDir::cleanPath(item.toLocalFile()));
        remember(path);
        if (rootOf(path).isEmpty()) {
            return {};
        }
        const auto known = m_states.constFind(path);
        if (known != m_states.constEnd()) {
            const QString emblem = emblemFor(known.value());
            return emblem.isEmpty() ? QStringList() : QStringList{emblem};
        }
        if (m_roots.contains(path)) {
            fetchRootStates();
        } else {
            fetchFolder(QFileInfo(path).path());
        }
        return {};
    }

private Q_SLOTS:
    void onStatesChanged(const QDBusMessage &message)
    {
        if (message.arguments().isEmpty()) {
            return;
        }
        const StateMap states = qdbus_cast<StateMap>(message.arguments().constFirst());
        for (auto it = states.constBegin(); it != states.constEnd(); ++it) {
            const QString path = realPath(QUrl(it.key()).toLocalFile());
            const QString folder = QFileInfo(path).path();
            if (m_states.contains(path) || m_folders.contains(folder) || m_roots.contains(path)) {
                setState(path, it.value());
            }
        }
    }

    void onRootChanged(const QString &uri)
    {
        if (uri.isEmpty()) {         // every root: Spola started, or the pairs changed
            forgetAll();
            fetchRoots();
            return;
        }
        const QString root = realPath(QUrl(uri).toLocalFile());
        const QList<QString> folders = m_folders.keys();
        for (const QString &folder : folders) {
            if (folder == root || folder.startsWith(root + QLatin1Char('/'))) {
                m_folders.remove(folder);
                fetchFolder(folder);   // ask again and push what changed
            }
        }
        fetchRootStates();
    }

private:
    void fetchRoots()
    {
        auto msg = QDBusMessage::createMethodCall(Service, ObjectPath, Interface, QStringLiteral("GetRoots"));
        auto *call = new QDBusPendingCallWatcher(m_bus.asyncCall(msg), this);
        connect(call, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            QDBusMessage reply = w->reply();
            if (reply.type() != QDBusMessage::ReplyMessage || reply.arguments().isEmpty()) {
                // Spola owns its name a moment before it answers: try again a little later
                static const int delays[] = {1, 2, 5, 10, 30};
                if (m_retry < 5) {
                    QTimer::singleShot(delays[m_retry++] * 1000, this, &SpolaOverlayPlugin::fetchRoots);
                }
                return;
            }
            m_retry = 0;
            m_roots.clear();
            const QDBusArgument arg = reply.arguments().constFirst().value<QDBusArgument>();
            arg.beginArray();
            while (!arg.atEnd()) {
                QString uri, pairId;
                arg.beginStructure();
                arg >> uri >> pairId;
                arg.endStructure();
                m_roots.insert(realPath(QDir::cleanPath(QUrl(uri).toLocalFile())));
            }
            arg.endArray();
            fetchRootStates();
            // what Dolphin asked about while Spola was away has no icon yet
            QSet<QString> folders;
            for (const QString &path : std::as_const(m_seen)) {
                if (!rootOf(path).isEmpty() && !m_roots.contains(path)) {
                    folders.insert(QFileInfo(path).path());
                }
            }
            for (const QString &folder : std::as_const(folders)) {
                fetchFolder(folder);
            }
        });
    }

    void fetchRootStates()
    {
        if (m_roots.isEmpty() || m_rootsInFlight) {
            return;
        }
        m_rootsInFlight = true;
        QStringList uris;
        for (const QString &root : std::as_const(m_roots)) {
            uris << QUrl::fromLocalFile(root).toString(QUrl::FullyEncoded);
        }
        auto msg = QDBusMessage::createMethodCall(Service, ObjectPath, Interface, QStringLiteral("GetStates"));
        msg << uris;
        auto *call = new QDBusPendingCallWatcher(m_bus.asyncCall(msg), this);
        connect(call, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            m_rootsInFlight = false;
            QDBusPendingReply<StateMap> reply = *w;
            if (reply.isError()) {
                return;
            }
            const StateMap states = reply.value();
            for (auto it = states.constBegin(); it != states.constEnd(); ++it) {
                setState(realPath(QUrl(it.key()).toLocalFile()), it.value());
            }
        });
    }

    void fetchFolder(const QString &folder)
    {
        if (m_inFlight.contains(folder) || rootOf(folder).isEmpty()) {
            return;
        }
        m_inFlight.insert(folder);
        auto msg = QDBusMessage::createMethodCall(Service, ObjectPath, Interface, QStringLiteral("GetFolderStates"));
        msg << QUrl::fromLocalFile(folder).toString(QUrl::FullyEncoded);
        auto *call = new QDBusPendingCallWatcher(m_bus.asyncCall(msg), this);
        connect(call, &QDBusPendingCallWatcher::finished, this, [this, folder](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            m_inFlight.remove(folder);
            QDBusPendingReply<StateMap> reply = *w;
            if (reply.isError()) {
                // known for a while, with nothing to show: not asked again at
                // every redraw of the folder, but once more in 30 s
                m_folders.insert(folder, ++m_age);
                QTimer::singleShot(30000, this, [this, folder] { m_folders.remove(folder); });
                return;
            }
            m_folders.insert(folder, ++m_age);
            const StateMap states = reply.value();
            for (auto it = states.constBegin(); it != states.constEnd(); ++it) {
                setState(folder + QLatin1Char('/') + it.key(), it.value());
            }
            dropOldFolders();
        });
    }

    void setState(const QString &path, uint state)
    {
        const auto old = m_states.constFind(path);
        if (old != m_states.constEnd() && old.value() == state) {
            return;
        }
        m_states.insert(path, state);
        const QString emblem = emblemFor(state);
        Q_EMIT overlaysChanged(QUrl::fromLocalFile(path), emblem.isEmpty() ? QStringList() : QStringList{emblem});
    }

    void forgetAll()
    {
        const QList<QString> paths = m_states.keys();
        m_states.clear();
        m_folders.clear();
        m_inFlight.clear();
        m_roots.clear();
        for (const QString &path : paths) {
            Q_EMIT overlaysChanged(QUrl::fromLocalFile(path), {});
        }
    }

    void dropOldFolders()
    {
        while (m_folders.size() > MaxFolders) {
            auto oldest = m_folders.constBegin();
            for (auto it = m_folders.constBegin(); it != m_folders.constEnd(); ++it) {
                if (it.value() < oldest.value()) {
                    oldest = it;
                }
            }
            const QString folder = oldest.key();
            m_folders.remove(folder);
            const QString prefix = folder + QLatin1Char('/');
            for (auto it = m_states.begin(); it != m_states.end();) {
                if (it.key().startsWith(prefix) && !it.key().mid(prefix.size()).contains(QLatin1Char('/'))) {
                    it = m_states.erase(it);
                } else {
                    ++it;
                }
            }
        }
    }

    void remember(const QString &path)
    {
        if (m_seen.contains(path)) {
            return;
        }
        if (m_seen.size() >= MaxSeen) {
            m_seen.clear();      // a rough bound: the visible items are asked again anyway
        }
        m_seen.insert(path);
    }

    QString rootOf(const QString &path) const
    {
        for (const QString &root : m_roots) {
            if (path == root || path.startsWith(root + QLatin1Char('/'))) {
                return root;
            }
        }
        return QString();
    }

    QDBusConnection m_bus;
    QSet<QString> m_roots;
    QHash<QString, uint> m_states;       // path -> state
    QHash<QString, quint64> m_folders;   // folders asked about -> age
    QSet<QString> m_inFlight;
    QSet<QString> m_seen;
    quint64 m_age = 0;
    int m_retry = 0;
    bool m_rootsInFlight = false;
};

#include "spolaoverlayplugin.moc"
